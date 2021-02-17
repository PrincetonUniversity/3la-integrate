"""
End-to-end example speech to text application in Relay.
Reads weights and biases from a Keras h5 file (model_lstm_256.h5)
"""
import sys

import tvm
from tvm import relay
from tvm import runtime

import numpy as np

def load_weights(data_file):
    from tensorflow import keras

    keras_model = keras.models.load_model(filename, custom_objects = {'<lambda>': lambda y_true, y_pred: y_pred})
    lstm_layer = keras_model.get_layer(name="rnn1")
    linear_layer = keras_model.get_layer(name="time_distributed_1")

    # need to transpose LSTM weights to be consistent with Relay interfaces
    # (can print the shapes to confirm this)
    lstm_i2h_w, lstm_h2h_w, lstm_bias = lstm_layer.get_weights()
    linear_w, linear_bias = linear_layer.get_weights()

    return (np.transpose(lstm_i2h_w), np.transpose(lstm_h2h_w), lstm_bias,
            np.transpose(linear_w), linear_bias)


def relay_lstm_cell(batch_size, input_size, hidden_size):
    # based on https://pytorch.org/docs/stable/generated/torch.nn.GRU.html#torch.nn.GRU
    state_tensor_type = relay.TensorType((batch_size, hidden_size))
    state_tuple_type = relay.TupleType([state_tensor_type, state_tensor_type])

    inp = relay.var("input", shape=(batch_size, input_size))
    state = relay.Var("state", type_annotation=state_tuple_type)

    w_ih = relay.var("w_ih", shape=(4*hidden_size, input_size))
    w_hh = relay.var("w_hh", shape=(4*hidden_size, hidden_size))
    b_ih = relay.var("b_ih", shape=(4*hidden_size,))
    b_hh = relay.var("b_hh", shape=(4*hidden_size,))

    hidden = relay.TupleGetItem(state, 0)
    cell_state = relay.TupleGetItem(state, 1)

    # PyTorch packs the i2h and h2h weights and biases together so we will match that here
    w_i_splits = relay.split(w_ih, 4, 0)
    w_h_splits = relay.split(w_hh, 4, 0)
    b_i_splits = relay.split(b_ih, 4, 0)
    b_h_splits = relay.split(b_hh, 4, 0)
    w_ii, w_if, w_ig, w_io = w_i_splits[0], w_i_splits[1], w_i_splits[2], w_i_splits[3]
    w_hi, w_hf, w_hg, w_ho = w_h_splits[0], w_h_splits[1], w_h_splits[2], w_h_splits[3]
    b_ii, b_if, b_ig, b_io = b_i_splits[0], b_i_splits[1], b_i_splits[2], b_i_splits[3]
    b_hi, b_hf, b_hg, b_ho = b_h_splits[0], b_h_splits[1], b_h_splits[2], b_h_splits[3]

    def weighted_value(weight, value, bias):
        return relay.transpose(relay.nn.dense(weight, value) + relay.reshape(bias, (hidden_size, 1)))

    i_t = relay.sigmoid(weighted_value(w_ii, inp, b_ii) + weighted_value(w_hi, hidden, b_hi))
    f_t = relay.sigmoid(weighted_value(w_if, inp, b_if) + weighted_value(w_hf, hidden, b_hf))
    g_t = relay.tanh(weighted_value(w_ig, inp, b_ig) + weighted_value(w_hg, hidden, b_hg))
    o_t = relay.sigmoid(weighted_value(w_io, inp, b_io) + weighted_value(w_ho, hidden, b_ho))
    c_t = f_t*cell_state + i_t*g_t
    h_t = o_t*relay.tanh(c_t)

    h_var = relay.Var("h")
    c_var = relay.Var("c")
    return relay.Function([inp, state, w_ih, w_hh, b_ih, b_hh],
                          relay.Let(h_var, h_t,
                                    relay.Let(c_var, c_t,
                                              relay.Tuple([h_var, relay.Tuple([h_var, c_var])]))),
                          ret_type=relay.TupleType([state_tensor_type, state_tuple_type]))


# Warning! This is an unrolled RNN! If you want a truly dynamic RNN,
# you should define it using a list ADT and apply the LSTM cell recursively.
# We can easily do that, though note that interacting
# with the ADT objects in the BYOC codegen would be tricky
def lstm_definition(batch_size, input_size, hidden_size, time_steps,
                    time_axis=1):
    state_tensor_type = relay.TensorType((batch_size, hidden_size))
    state_tuple_type = relay.TupleType([state_tensor_type, state_tensor_type])

    input_var = relay.var("input", shape=(batch_size, time_steps, input_size))
    state_var = relay.var("state", type_annotation=state_tuple_type)
    i2h_weight_var = relay.var("i2h_weight", shape=(4*hidden_size, input_size))
    h2h_weight_var = relay.var("h2h_weight", shape=(4*hidden_size, hidden_size))
    i2h_bias_var = relay.var("i2h_bias", shape=(4*hidden_size,))
    h2h_bias_var = relay.var("h2h_bias", shape=(4*hidden_size,))

    # in this case, we are ignoring the state outputs
    builder = relay.ScopeBuilder()
    cell_var = builder.let("lstm_cell", relay_lstm_cell(batch_size, input_size, hidden_size))
    splits = builder.let("splits", relay.split(input_var, time_steps, time_axis).astuple())
    last_state = state_var
    seq_outs = []
    for i in range(time_steps):
        squeezed = builder.let(f"squeezed_{i}", relay.squeeze(relay.TupleGetItem(splits, i), axis=[time_axis]))
        cell_out = builder.let(f"cell_out_{i}",
                               cell_var(squeezed, last_state,
                                        i2h_weight_var, h2h_weight_var,
                                        i2h_bias_var, i2h_bias_var))
        new_seq_out = builder.let(f"seq_out_{i}", relay.TupleGetItem(cell_out, 0))
        seq_outs.append(new_seq_out)
        new_hidden = builder.let(f"state_update_{i}", relay.TupleGetItem(cell_out, 1))
        last_state = new_hidden

    stacked = builder.let("stacked", relay.stack(seq_outs, axis=time_axis))
    # finally reshape to match pytorch's semantics (one layer)
    reshape_hidden = builder.let("final_hidden",
                                 relay.reshape(relay.TupleGetItem(last_state, 0),
                                               (1, batch_size, hidden_size)))
    reshape_cell = builder.let("final_cell",
                               relay.reshape(relay.TupleGetItem(last_state, 1),
                                             (1, batch_size, hidden_size)))
    builder.ret(relay.Tuple([stacked, reshape_hidden, reshape_cell]))

    ret_type = relay.TupleType([
        relay.TensorType((batch_size, time_steps, hidden_size)),
        relay.TensorType((1, batch_size, hidden_size)),
        relay.TensorType((1, batch_size, hidden_size))
    ])

    return relay.Function([input_var, state_var, i2h_weight_var, h2h_weight_var,
                           i2h_bias_var, h2h_bias_var],
                          builder.get(),
                          ret_type=ret_type)


def linear_layer_definition(batch_size, hidden_size, dense_dim):
    input_var = relay.var("input", shape=(batch_size, hidden_size))
    weight_var = relay.var("weight", shape=(dense_dim, hidden_size))
    bias_var = relay.var("bias", shape=(dense_dim,))

    return relay.Function([input_var, weight_var, bias_var],
                          relay.nn.bias_add(
                              relay.nn.dense(input_var, weight_var),
                              bias_var),
                          ret_type=relay.TensorType((batch_size, dense_dim)))


def build_relay_module(batch_size, input_size, hidden_size, time_steps, dense_dim):
    mod = tvm.IRModule()
    mod["lstm_layer"] = lstm_definition(batch_size, input_size, hidden_size, time_steps)
    mod["linear_layer"] = linear_layer_definition(batch_size, hidden_size, dense_dim)
    lstm_var = mod.get_global_var("lstm_layer")
    linear_var = mod.get_global_var("linear_layer")

    # now we build up our main function
    input_var = relay.var("input", shape=(batch_size, time_steps, input_size))
    init_hidden_var = relay.var("init_hidden", shape=(batch_size, hidden_size))
    init_cell_var = relay.var("init_cell", shape=(batch_size, hidden_size))
    i2h_weight_var = relay.var("i2h_weight", shape=(4*hidden_size, input_size))
    h2h_weight_var = relay.var("h2h_weight", shape=(4*hidden_size, hidden_size))
    lstm_bias_var = relay.var("lstm_bias", shape=(4*hidden_size,))
    linear_weight_var = relay.var("linear_weight", shape=(dense_dim, hidden_size))
    linear_bias_var = relay.var("linear_bias", shape=(dense_dim,))

    builder = relay.ScopeBuilder()
    state_var = builder.let("state", relay.Tuple([init_hidden_var, init_cell_var]))
    lstm_res = builder.let("lstm_res",
                           lstm_var(input_var, state_var,
                                    i2h_weight_var, h2h_weight_var,
                                    lstm_bias_var,
                                    # the keras model only gave one bias,
                                    # so set the other to zero
                                    # (hopefully this is correct)
                                    relay.zeros_like(lstm_bias_var)))
    final_hidden = builder.let("final_hidden",
                               relay.TupleGetItem(lstm_res, 1))
    # to match PT's semantics, we're undoing the reshape in LSTM :)
    reshape_hidden = builder.let("reshape_hidden",
                                 relay.squeeze(final_hidden, axis=[0]))
    linear_result = builder.let("linear_result",
                                linear_var(reshape_hidden,
                                           linear_weight_var, linear_bias_var))
    # finally do a softmax
    builder.ret(relay.nn.softmax(linear_result))
    main_func = relay.Function([input_var, init_hidden_var, init_cell_var,
                                i2h_weight_var, h2h_weight_var, lstm_bias_var,
                                linear_weight_var, linear_bias_var],
                               builder.get())
    mod["main"] = main_func
    return mod


def main(data_file, batch_size, time_steps, seed):
    lstm_i2h_w, lstm_h2h_w, lstm_bias, linear_w, linear_bias = load_weights(data_file)
    i2h_shape = np.shape(lstm_i2h_w)
    assert len(i2h_shape) == 2
    # LSTM packs in four separate weights into one tensor
    print(i2h_shape)
    hidden_size = i2h_shape[0] // 4
    input_size = i2h_shape[1]
    linear_shape = np.shape(linear_bias)
    assert len(linear_shape) == 1
    dense_dim = linear_shape[0]

    mod = build_relay_module(batch_size, input_size, hidden_size, time_steps, dense_dim)

    # generate initial values for the input_vars
    if seed is not None:
        np.random.seed(seed)
    random_input = np.random.rand(batch_size, time_steps, input_size)
    random_hidden = np.random.rand(batch_size, hidden_size)
    random_cell = np.random.rand(batch_size, hidden_size)

    args = map(lambda a: a.astype("float32"), [
        random_input, random_hidden, random_cell,
        lstm_i2h_w, lstm_h2h_w, lstm_bias, linear_w, linear_bias
    ])

    target = 'llvm'
    ctx = tvm.cpu(0)
    with tvm.transform.PassContext(opt_level=3):
        exe = relay.vm.compile(mod, target)
        vm = runtime.vm.VirtualMachine(exe, ctx)
        ret = vm.invoke("main", *[tvm.nd.array(arg, ctx=ctx) for arg in args])
    print(ret.asnumpy())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 end_to_end_speech_to_text.py data_file.h5  batch_size[default: 1] time_steps[default: 5] [optional: seed]")
        exit()
    filename = sys.argv[1]
    batch_size = 1
    time_steps = 5
    seed = None
    if len(sys.argv) >= 3:
        batch_size = int(sys.argv[2])
    if len(sys.argv) >= 4:
        time_steps = int(sys.argv[3])
    if len(sys.argv) >= 5:
        seed = int(sys.argv[4])
    main(filename, batch_size, time_steps, seed)
