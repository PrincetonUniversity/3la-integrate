# 3LA Integration 

This is to integrate all parts (e.g., TVM-BYOC and ILAng generated simulators) into a single Docker image.

## Build the docker image

RSA key-pair is required to access the private repos. For password protected key-pair:

``` bash
docker build --build-arg SSH_KEY="$(openssl rsa -in ~/.ssh/id_rsa)" --tag byo3la --file Dockerfile .
```

For non-password-protected key-pair:

``` bash
docker build --build-arg SSH_KEY="$(cat ~/.ssh/id_rsa)" --tag byo3la --file Dockerfile .
```

To remove intermediate images (licensed packages and your private keys):

``` bash
docker rmi -f $(docker images -q --filter label=stage=intermediate)
```

## TODO

The TVM part is now building a previous fork as a placeholder. Replace the link and add options/flags as needed for the 3LA-capable TVM.
