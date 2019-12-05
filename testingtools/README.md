# testingtools
- [collect_support_bundle.py](#collect_support_bundle_py)
- [deploy_app_cluster.py](#deploy_app_cluster_py)
- [deploy_fio.py](#deploy_fio_py)
- [deploy_nuvo_kontroller.py](#deploy_nuvo_kontroller_py)
- [rei.py](#rei_py)

## collect_support_bundle.py
*TBD*

## deploy_app_cluster.py
*TBD*

## deploy_fio.py
*TBD*

## deploy_nuvo_kontroller.py
*TBD*

## rei.py
This script is used to manage runtime error injection into
[kontroller](https://github.com/Nuvoloso/kontroller) daemon processes.
See [Runtime error injection](https://tinyurl.com/y5efz4zr) for more details on REI support
in [kontroller](https://github.com/Nuvoloso/kontroller).
Create the python3 virtual environment before using the script:
```
testing$ make venv
...
testing$ . venv/bin/activate
(venv) testing$ cd testingtools
```

To inject an error into **centrald**:
```
(venv) testingtools$ ./rei.py -n sreq/detach-storage-fail
```

To inject an error into **clusterd**, specify the `-C` flag:
```
(venv) testingtools$ ./rei.py -n vreq/publish-sp-fail
```

To inject an error into **agentd**, specify the `-C` flag and the pod name:
```
(venv) testingtools$ ./rei.py -C -p nuvoloso-node-h7lv8 -n vreq/foo
```
To make the error persist, specify the number of times with the `-u` flag or
make it permanent with the `-d` flag:
```
(venv) testingtools$ ./rei.py -C -n vreq/size-block-on-start -d
(venv) testingtools$ ./rei.py -n vreq/ac-fail-spa-create -u 2
```

The examples above were for *boolean* values.
All supported types can be set. For example:
```
(venv) testingtools$ ./rei.py -C -n vreq/cg-snapshot-pre-wait-delay -i 30
```
will set an integer value.

To remove an injected error file add the `-r` flag.
```
(venv) testingtools$ ./rei.py -C -n vreq/size-block-on-start -r
```

Run the command help (`-h`) for more details.
