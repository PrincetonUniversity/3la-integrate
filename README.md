# 3LA Integration 

This is to integrate all parts (e.g., TVM-BYOC and ILAng generated simulators) into a single Docker image.

## Build the docker image

RSA key-pair is required to access the private repos. For password protected key-pair:

``` bash
docker build --build-arg SSH_KEY="$(openssl rsa -in ~/.ssh/id_rsa)" --tag byo3la --file Dockerfile .
docker rmi -f $(docker images -q --filter label=stage=intermediate)
```

For non-password-protected key-pair:

``` bash
docker build --build-arg SSH_KEY="$(cat ~/.ssh/id_rsa)" --tag byo3la --file Dockerfile .
docker rmi -f $(docker images -q --filter label=stage=intermediate)
```

