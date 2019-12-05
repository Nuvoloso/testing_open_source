#/usr/bin/python3
"""

Copyright 2019 Tad Lebeck
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Deploys an application container with fio. This custom-fio image is in dockerhub.
IO runs against Nuvo volume that is mounted on "/datadir".
This is a long running test and can be controlled by RUNTIME param.
Test exits after launching the fio app container.

Dependencies:
It needs the Nuvo Kontroller hostname as input. To deploy one, use deploy_nuvo_kontroller.py
PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib)


Example of a run command:
 python3 testing/scripts/fio_multivols.py --kops_cluster_name nuvodata.k8s.local  --nodes 3 \
 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIXXKA  \
 --aws_secret_access_key 1vdXXX+A --region us-west-2 \
 --nuvo_kontroller_hostname a22b607.us-west-2.elb.amazonaws.com  --volume_sizes 20,30,40

It exits after all the fio containers (1 per volume) have reached 'Completed' state.

Detailed steps:
1. Reads input params, including '--volume_sizes'. The number of volume_sizes dictates how many
volumes (and fio containers) would be deployed in a run. Default: 'default="20,30,40"' means three
volumes with 20G, 30G and 40G size (so, three fio containers with one volume each).
2. Installs kops, kubectl, awscli
3. Configures aws. This step is skipped if '.aws/credentials' exists. Otherwise, it creates 'config'
and 'credentials' files under '.aws' directory.
4. Generates ssh-keypair unless '/.ssh/id_rsa' already exists
5. Runs 'kops create cluster' command without '--yes' option to check setup is valid.
6. Runs 'kops update cluster' (and 'kops update cluster', if needed) with '--yes' and loops/waits
till dataplane (aka nuvo) cluster is ready.
7. Calls Nuvo Management APIs to:
    create a CSP domain, then,
    deploy nuvo cluster namespace (i.e. '-n nuvolsolo-cluster'),
    does Service Plan Allocation,
8. Then, for each vol_size:
        - creates a nuvo volume object, then,
        - launches pvc for that nuvo volume,
        - creates a new fiotest-xxx.yaml file by copying the template (testing/config/fiotest.yaml)
        - and filling the FIO params (e.g. fio_readpercent, fio_writepercent, fio_numjobs etc.)
        - using the inputs to the test. This launches 1 pod with 1 container (with fio running IO
        to nuvo volume)
        - launches an new fio container with the pvc. Waits for the container to go to 'Running'
        state
9. After all fio containers (1 per 'volume_sizes') are in 'Running' state, it then:
        - checks the state of all user pods/containers. If all of them have reached 'Completed'
        - state (OR if max_timeout is reached), then exits. Otherwise,
        - sleeps (default: 1200 seconds). Here, it logs o/p of:
            - "ps -ef | grep fio" (for each container),
            - "ls -lrt " + FIO_MOUNTPATH,
            - kubectl logs of the fio container.
        Note: Maximum time, the test will wait for all three containers to go to 'Completed'
        state is derived from the job file specification or defaults to 2 hrs per job. This is needed
        because the latter containers take much longer to start (CUM-1600).
10. If all the pods 'Completed' - Success, else raises an exception, "Some/all pods did not reach
'Completed' state within time limit". IMPORTANT: Any exception and its backtrace will appear in
the 'Console log' of a test's run in Jenkins (NOT in the test's log). 
"""

import argparse
import collections
import datetime
import itertools
import logging
import os
import pathlib
import time
import subprocess
import uuid
import yaml
import configparser

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement
from nuvoloso.nuvoloso.connect_ssh import ConnectSSH
from nuvoloso.api.obj_helper import VolumeSeriesRequestObject, VolumeSeriesObject

CHECK_OUTPUT_TIMEOUT = 600
DEFAULT_CENTRALD_CLUSTER_NAME = "nuvotestfiomulti.k8s.local"
FIO_MOUNTPATH = "/datadir"
FIO_MOUNTNAME = "datavol"
NODE_LABEL = "fionode"
DEFAULT_TIMEOUT = 7200
TEST_RUNTIME_DELTA = 18000
USERAPP_FILENAME = "fiotest.yaml"
WAIT_TIMEOUT = 1200
VOLUME_CLAIM_SUFFIX = "c"
COPY_DIRECTORY = "/tmp"
# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

class TestRunFioMultiVols(object):
    """Deploys Fio app container that runs IO against NUVO volume"""

    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args, args.kops_cluster_name)
        self._mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
        self.connect_ssh = ConnectSSH(self.args)

    def dict_representer(self, dumper, data):
        """Yaml representer"""

        return dumper.represent_mapping(self._mapping_tag, data.items())

    def dict_constructor(self, loader, node):
        """Yaml constructor"""

        return collections.OrderedDict(loader.construct_pairs(node))

    def write_fio_yamlfile(self, iteration_number, volume_name, pvc_name):
        """Create yaml with fio's input params"""

        yaml.add_representer(collections.OrderedDict, self.dict_representer)
        yaml.add_constructor(self._mapping_tag, self.dict_constructor)

        app_orig_yaml_path = self.args.config_dirpath + "/" + USERAPP_FILENAME
        app_yaml_path = self.args.log_dirpath + "/" + USERAPP_FILENAME.split(".")[0] + "-" + \
            volume_name + str(iteration_number) +  ".yaml"
        logging.info("app_yaml_path: %s", app_yaml_path)

        #pvc_name = self.args.fio_mountname + str(iteration_number) + VOLUME_CLAIM_SUFFIX
        dicts = []
        with open(app_orig_yaml_path, 'r') as stream, open(app_yaml_path, 'w') as outfile:
            dicts = list(yaml.load_all(stream))
            for yaml_dict in dicts:
                if yaml_dict['kind'] == 'Pod':
                    yaml_dict['metadata']['name'] = self.args.fio_name + str(iteration_number)
                    yaml_dict['metadata']['labels']['name'] = \
                        self.args.fio_name + str(iteration_number)
                    yaml_dict['spec']['containers'][0]['name'] = \
                        self.args.fio_name + str(iteration_number)
                    yaml_dict['spec']['containers'][0]['image'] = self.args.fio_image
                    yaml_dict['spec']['containers'][0]['env'][0]['value'] = FIO_MOUNTPATH
                    yaml_dict['spec']['containers'][0]['env'][1]['value'] = self.args.fio_jobfile.split('/')[-1]
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['name'] = \
                        FIO_MOUNTNAME
                        #self.args.fio_mountname
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['mountPath'] = \
                        FIO_MOUNTPATH
                        #self.args.fio_mountpath
                    yaml_dict['spec']['volumes'][0]['name'] = \
                        FIO_MOUNTNAME
                        #self.args.fio_mountname
                    yaml_dict['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = pvc_name
                    yaml_dict['spec']['volumes'][1]['hostPath']['path'] = COPY_DIRECTORY
                elif yaml_dict['kind'] == 'PersistentVolumeClaim':
                    # PVC changes
                    raise Exception("Yaml template seems old/deprecated")
                else:
                    raise Exception("Yaml template file: %s is unsupported" % app_orig_yaml_path)
            yaml.dump_all(dicts, outfile, default_flow_style=False)
        return app_yaml_path

    def check_pods(self):
        """Check fio is running fine in all pods"""

        # We should have 1 pod for each volume.
        num_pods_done = 0
        for i in range(0, len(self.args.list_volume_sizes)):
            pod_name = self.args.fio_name + str(i)
            try:
                logging.info("------ Start checking Pod Name: %s ------", pod_name)
                self.kubectl_helper.check_pod_running(pod_name)

                # Check fio showsup in 'ps -ef' command inside the container
                cmd_ps = "ps -ef | grep fio"
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ps, pod_name, pod_name)
                logging.info(result)
                if not result or "fio" not in result:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s" % (cmd_ps, pod_name))
                logging.info("O/P of 'ps -ef' cmd shows fio is running in pod: %s", pod_name)

                # Show timestamp on fio o/p file
                cmd_ls = "ls -lrt " + FIO_MOUNTPATH
                result = self.kubectl_helper.run_kubectl_exec_cmd(cmd_ls, pod_name, pod_name)
                logging.info(result)
                if not result or len(result) < 1:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s. Result: %s" % (cmd_ls, pod_name, result))
                logging.info("ls -lrt of 'datadir' ran fine in pod: %s", pod_name)

                # Show container's logs
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, pod_name)
                logging.info(result)
                if not result or "error" in result:
                    raise Exception("No output OR found 'error' while collecting logs "
                                    "from pod: %s" % pod_name)
                logging.info("kubectl log shows fio stats - fine in pod: %s",
                             pod_name)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
            except subprocess.CalledProcessError as err:
                time.sleep(5)
                if not self.kubectl_helper.check_pod_completed(pod_name):
                    raise
                logging.info(err.output)
                logging.info("Pod name: %s has completed its run. Container's "
                             "log:", pod_name)
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, pod_name)
                logging.info(result)
                num_pods_done = num_pods_done + 1
                logging.info("num_pods_done: %d", num_pods_done)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
                if i == (len(self.args.list_volume_sizes) - 1):
                    # we're done checking all pods
                    if num_pods_done == len(self.args.list_volume_sizes):
                        return True
                    else:
                        return False
                else:
                    # check remaining pods
                    pass

    def label_all_nodes(self):
        """Label each of the nodes in the cluster with a unique string"""

        dict_label_node = {}
        node_list = self.kubectl_helper.get_nodes_cluster(self.args.kops_cluster_name)
        for i in range(len(node_list)):
            self.kubectl_helper.label_nodes(node_name=node_list[i], label_value=NODE_LABEL + str(i))
            # e.g. 'fionode0': 'ip-172-20-39-181.us-west-2.compute.internal'
            dict_label_node[NODE_LABEL + str(i)] = node_list[i]
        return dict_label_node

    def install_dependencies(self):
        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        return

    def create_application_cluster(self):
        KopsCluster.create_kops_app_cluster(self.args)
        try:
            csp_domain_id = self.nuvo_mgmt.create_csp_domain()
            nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_domain_id)
            logging.info("nuvo_cluster_name: %s", nuvo_cluster_name)
            self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
            protection_domain_id = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_protection_domain(protection_domain_id, csp_domain_id)
            snapshot_catalog_pd = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_snapshot_catalog_policy(snapshot_catalog_pd, csp_domain_id)
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise
        return nuvo_cluster_name

    def wait_for_snapshot_completion(self, volume_name):
        vs_obj = VolumeSeriesObject()
        vs_obj.find(self.nuvo_mgmt, volume_name)
        unmount_vsr = VolumeSeriesRequestObject()
        max_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=TEST_RUNTIME_DELTA)
        while datetime.datetime.utcnow() <= max_time:
            tmp = unmount_vsr.query(self.nuvo_mgmt, dict(volumeSeriesId=vs_obj.meta_id(), requestedOperations="UNMOUNT"))
            if tmp:
                unmount_vsr = tmp[-1]
                break
            time.sleep(self.args.fio_sleep_time)
        unmount_vsr.wait(self.nuvo_mgmt)
        unmount_vsr.load(self.nuvo_mgmt)
        if unmount_vsr._data['volumeSeriesRequestState'] != "SUCCEEDED":
            raise RuntimeError("Unmount was not successful: %s", unmount_vsr._data)
        vsr_obj = VolumeSeriesRequestObject()
        vsr_obj = vsr_obj.query(self.nuvo_mgmt, dict(volumeSeriesId=vs_obj.meta_id(), requestedOperations="VOL_SNAPSHOT_CREATE"))[-1]
        vsr_obj.wait(self.nuvo_mgmt)
        vsr_obj.load(self.nuvo_mgmt)
        if vsr_obj._data['volumeSeriesRequestState'] != "SUCCEEDED":
            raise RuntimeError("Snapshot creation was not successful: %s", vsr_obj._data)
        return

    def run_fio_multiple_vols(self):
        """Run fio app with multiple containers/volumes - long running test"""

        try:
            nuvo_cluster_name = self.args.nuvo_cluster_name
            spa_id = self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, self.args.account_name)
            self.nuvo_mgmt.launch_secret_yaml(nuvo_cluster_name)
            volume_list = []

            # Label the nodes in the cluster
            dict_label_node = self.label_all_nodes()
            if not dict_label_node:
                raise Exception("dict_label_node is empty. Failed to label the nodes..")
            logging.info("dict_label_node: %s", dict_label_node)
            for node_label in dict_label_node.keys():
                hostname = self.kubectl_helper.get_node_external_dns("nodekey", node_label)
                logging.info("Copying to %s", hostname)
                self.connect_ssh.scp_copy_to_node(hostname, self.args.fio_jobfile, COPY_DIRECTORY)

            for i in range(0, len(self.args.list_volume_sizes)):
                # Create volumes
                logging.info("Iteration number: %d, Creating volume of size: "
                             "%s ...", i, self.args.list_volume_sizes[i])

                if not self.args.static:
                    logging.info("Creating a dynamically provisioned volume")
                    # volume name will be dynamic
                    # need to get YAML from pool
                    pvc_name_prefix="nuvoauto-pvc-"
                    pvc_name = pvc_name_prefix + str(uuid.uuid4())[:5]
                    # launch volume in k8s
                    volume_name = self.nuvo_mgmt.launch_volume_pvc_dynamic(pvc_name, spa_id, vol_size=self.args.list_volume_sizes[i])
                    vol_id = None
                else:
                    logging.info("Creating a static volume")
                    volume_name = self.nuvo_mgmt.create_volume(nuvo_cluster_name,
                                                        vol_size=self.args.list_volume_sizes[i])
                    # launch volume in k8s
                    pvc_name = self.nuvo_mgmt.launch_volume_pvc(volume_name)
                    vol_id = self.nuvo_mgmt.get_volume_id(volume_name)
                
                logging.info("volume_name: %s, pvc_name: %s", volume_name, pvc_name)
                volume_list.append(volume_name)

                # create the app yaml file based off template
                app_yaml_path = self.write_fio_yamlfile(i, volume_name, pvc_name)
                pod_name = self.args.fio_name + str(i)
                self.kubectl_helper.deploy_appcontainer(app_yaml_path, volume_name,
                    pod_name=pod_name, pvc_name=pvc_name, dynamic=not self.args.static, vol_id=vol_id)
                curr_node = self.kubectl_helper.get_scheduled_node(pod_name)

                curr_label = self.kubectl_helper.get_node_label("nodekey", curr_node)
                if not self.kubectl_helper.check_pod_node_assignment(pod_name,
                                                                     dict_label_node[curr_label]):
                    raise Exception("pod_name: %s was NOT deployed on node_name: %s", pod_name,
                                    dict_label_node[curr_label])
            # Now we wait/poll till RUNTIME is over to make sure fio is running success
            all_pods_done = False
            time_now = datetime.datetime.utcnow()
            while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                    seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
                logging.info("========================")
                if self.check_pods():
                    all_pods_done = True
                    logging.info("All pods have completed successfully.")
                    break
                else:
                    logging.info("Sleeping for %s before checking the pods again...",
                                 self.args.fio_sleep_time)
                    time.sleep(self.args.fio_sleep_time)

            if all_pods_done:
                logging.info("SUCCESS: All pods with fio ran fine.")
                result = self.kubectl_helper.get_pods()
                if result:
                    logging.info(result)
                for pod in result.splitlines():
                    pod_name = pod.split()[0]
                    if self.args.fio_name in pod_name:
                        self.kubectl_helper.delete_pod(pod_name)
                for volume in volume_list:
                    self.wait_for_snapshot_completion(volume)
            else:
                raise Exception("Some/all pods did not reach 'Completed' state within time limit")
            # cleanup
            if self.args.do_cleanup:
                logging.info("Test succeeded. Deleting kops cluster now")
                KopsCluster.kops_delete_cluster(kops_cluster_name=self.args.kops_cluster_name,
                                                kops_state_store=self.args.kops_state_store)
                domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.args.nuvo_cluster_name)
                self.nuvo_mgmt.wait_for_cluster_state(self.args.nuvo_cluster_name, "TIMED_OUT", domain_id=domain_id)
                self.kubectl_helper.cleanup_cluster(self.args.nuvo_cluster_name, \
                    self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'] , self.args.account_name)
            else:
                logging.info("Test succeeded. Skipping kops delete cluster since "
                             "do_cleanup is False")
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise
        except:
            raise


def get_default_timeout(jobfile):
    """
    For every job, add up 'runtime' if specified, or else add up if a global runtime if specified or else add up DEFAULT_TIMEOUT
    """

    try:
        parser = configparser.ConfigParser(strict=False)
        parser.read(jobfile)
    except configparser.MissingSectionHeaderError as err:
        logging.exception("Fio job file format is not valid INI: %s", err.output)
        raise
    except configparser.ParsingError:
        pass

    runtime = 0
    global_runtime = None
    for s in parser.sections():
        if s == 'global':
            if 'runtime' in parser[s].keys():
                global_runtime = int(parser[s]['runtime'])
        else:
            if 'runtime' in parser[s].keys():
                runtime += int(parser[s]['runtime'])
            else:
                runtime += global_runtime if global_runtime else DEFAULT_TIMEOUT
    return runtime if runtime > 0 else DEFAULT_TIMEOUT

def main():
    """main"""

    default_cluster_name = DEFAULT_CENTRALD_CLUSTER_NAME
    default_aws_access_key = default_aws_secret_access_key = default_kops_state_store = None

    if os.environ.get('KOPS_CLUSTER_NAME'):
        default_cluster_name = os.environ.get('KOPS_CLUSTER_NAME')

    if os.environ.get('KOPS_STATE_STORE') is None:
        default_kops_state_store = os.environ.get('KOPS_STATE_STORE')

    if os.environ.get('AWS_ACCESS_KEY_ID'):
        default_aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')

    if os.environ.get('AWS_SECRET_ACCESS_KEY') is None:
        default_aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo data plane and runs fio against all volumes")
    parser.add_argument(
        '--kops_cluster_name', help='name of kops cluster for Nuvo Data Plane[default: ' +
        default_cluster_name + ']',
        default=default_cluster_name)
    parser.add_argument(
        '--nodes', help='number of nodes in the cluster [default=3]', type=int, default=3,
        choices=range(1, 101))
    parser.add_argument(
        '--kops_state_store', help='state store for cluster',
        default=default_kops_state_store)
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey', default=default_aws_access_key)
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey',
        default=default_aws_secret_access_key)
    parser.add_argument(
        '--region', help='aws region', default=None)
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node',
        default=None)
    parser.add_argument(
        '--k8s_nodes_zone', help='aws zone for other nodes ',
        default=None)
    parser.add_argument(
        '--master_size', help='ec2 instance type for master node ', default=None)
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes ', default=None)
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--config_dirpath', help='directory path to user-app yaml files.'
        '[default=${HOME}/testing/config/')
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Demo Tenant')
    parser.add_argument(
        '--volume_sizes', help='Comma separated list of volume sizes', default="20,30,40")
    parser.add_argument(
        '--fio_name', help='Name of fio container [default=fiotest]', default='fiotest')
    parser.add_argument(
        '--fio_image', help='Path to Docker image for fio container '
        '[default=nuvolosotest/fiotest:v1]', default='407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v2')
    parser.add_argument(
        '--fio_jobfile', help='Path to an fio job file [default=${HOME}/testing/config/sanity_jobfile.fio]')
    parser.add_argument(
        '--fio_timeout', help='Time in seconds, in addtion to a delta of {0}s to wait for the fio job(s) to complete.'.format(TEST_RUNTIME_DELTA))
    parser.add_argument(
        '--fio_sleep_time', help='Sleep in seconds [default=1200] '
        'before checking the pods again', type=int, default=1200)
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--do_cleanup', help='Delete kops cluster of data plane in the end',
        action='store_true', default=False)
    parser.add_argument(
        '--kubernetes_version', help='version of kubernetes to deploy', default=None)
    parser.add_argument(
        '--image', help='AMI Image for all instances', default=DEFAULT_AMI)
    parser.add_argument(
        '--node_volume_size', help='volume size for slave nodes of k8s cluster', type=int,
        default=10)
    parser.add_argument(
        '--master_volume_size', help='volume size for master node of k8s cluster', type=int,
        default=20)
    parser.add_argument(
        '--serviceplan_name', help='Name of service plan ', default=None)
    parser.add_argument(
        '--static', help='Pass flag to use pre-provisioned volumes', action='store_true')
    parser.add_argument(
        '--nuvo_cluster_name', help='Nuvo cluster name already created. \
                Passing this argument assumes that the application cluster is already created', default=None)

    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"

    home_dir = str(pathlib.Path.home())
    if not args.config_dirpath:
        args.config_dirpath = str(pathlib.Path(__file__).parent.absolute()/'../config/')
    if not args.fio_jobfile:
        args.fio_jobfile = str(pathlib.Path(__file__).parent.absolute()/'../config/sanity_jobfile.fio')
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    args.list_volume_sizes = args.volume_sizes.strip().split(",")
    assert(args.list_volume_sizes), "'volume_sizes' param was empty"
    logging.info("len args.list_volume_sizes: %d", len(args.list_volume_sizes))
    logging.info("args.list_volume_sizes: %s", args.list_volume_sizes)
    if not args.fio_timeout:
        args.fio_timeout = get_default_timeout(args.fio_jobfile)
    logging.info("args.fio_timeout: %s", args.fio_timeout)
    logging.info("args.fio_jobfile: %s", args.fio_jobfile)
    logging.info(args)

    print("Script to Run FIO against a NUVO volume - Long Running test")
    test = TestRunFioMultiVols(args)
    if not args.nuvo_cluster_name:
        logging.info("Creating Application cluster")
        test.install_dependencies()
        args.nuvo_cluster_name = test.create_application_cluster()
    test.run_fio_multiple_vols()


if __name__ == '__main__':
    main()
