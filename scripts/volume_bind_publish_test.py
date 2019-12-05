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


This script tests the volume bind, publish workflow.

Prerequisites : 
1. Requires a kontroller hostname. A K8s cluster with the kontroller software running should be deployed.
To deploy one, use deploy_nuvo_kontroller.py
2. PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib) 
3. Optionally, application cluster(s) can be deployed as well using deploy_app_cluster.py

Example:

1. If app cluster is not already created:
PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/volume_bind_publish_test.py --nuvo_kontroller_hostname aee29e34cb86611e9a9500a6e6eb7226-1601001206.us-west-2.elb.amazonaws.com \
    --app_cluster1 "newtest.k8s.local" --app_cluster2 "newtest2.k8s.local" --aws_access_key AKIA2LFL5KHLLQERWT \
    --aws_secret_access_key wPXm/8L7zyAZShXpqZmpEru8Oesdgsgg044245@235 --nodes 1 --kops_state_store s3://nri-test-bucket

2. If app cluster is already deployed :
PYTHONPATH=~/testing/lib/ python3 ~/testing/scripts/volume_bind_publish_test.py --nuvo_kontroller_hostname aee29e34cb86611e9a9500a6e6eb7226-1601001206.us-west-2.elb.amazonaws.com \
    --app_cluster1 "newtest.k8s.local" --app_cluster2 "newtest2.k8s.local" --aws_access_key AKIA2LFL5KHSDGGSGG \
    --aws_secret_access_key wPXm/8L7zyAZSOeWAhAiuqZnnfmLJwgwg4/Sdgs235 --nodes 1 --kops_state_store s3://nri-test-bucket --nuvo_cluster1 nuvoauto-87f33 --nuvo_cluster2 nuvoauto-92s14

Simply put, if nuvo_cluster1 and/or nuvo_cluster2 options are provided, the test wont attempt to create app_cluster1 and/or app_cluster2 resp.

Test steps:
1. Install dependencies and create one or more app clusters, if required.
2. For every volume size specified, deploy an fio container on app_cluster1 which does writes on a dynamically provisioned volume.
3. Once all fio pods are successful, delete the pods and the pvc.
4. Check for the state of the volume(s) created in step 2. Wait for volume to go to UNBOUND state. By this time the volume snapshots would be created and uploaded.
5. Create a volume series request to BIND and PUBLISH to app_cluster2. This should bind the previously created volumes as pre-provisioned volumes in app_cluster2
6. Deploy fio containers, making use of the pre-provisioned volumes, to read and verify data that was written in step 2.
7. If clean up flag is enabled, delete the application clusters or else just delete all pods that were previously created.

"""

import argparse
import collections
import datetime
import logging
import os
import pathlib
import time
import subprocess
import uuid
import yaml
import json

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement
from nuvoloso.nuvoloso.connect_ssh import ConnectSSH

FIO_MOUNTPATH = "/datadir"
FIO_MOUNTNAME = "datavol"
TEST_RUNTIME_DELTA = 10800
APP_YAML_TEMPLATE = "fioverify.yaml"
WRITE_JOB = "write_verify.fio"
READ_JOB = "read_verify.fio"
COPY_DIRECTORY = "/tmp"
# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

class TestBindPublish(object):
    """Deploys Fio app container that runs IO against NUVO volume"""

    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = dict([
            (args.app_cluster1, KubectlHelper(args, args.app_cluster1)), 
            (args.app_cluster2, KubectlHelper(args, args.app_cluster2))
        ])
        self._mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
        self.connect_ssh = ConnectSSH(self.args)

    def dict_representer(self, dumper, data):
        """Yaml representer"""

        return dumper.represent_mapping(self._mapping_tag, data.items())

    def dict_constructor(self, loader, node):
        """Yaml constructor"""

        return collections.OrderedDict(loader.construct_pairs(node))

    def install_dependencies(self):
        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        return

    def create_application_cluster(self, app_cluster_name):
        self.args.kops_cluster_name = app_cluster_name
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

    def copy_jobfile_to_nodes(self, jobfile_name, cluster_name):
        node_list = self.kubectl_helper[cluster_name].get_nodes_cluster()
        for node in node_list:
            hostname = self.kubectl_helper[cluster_name].get_node_external_dns("kubernetes.io/hostname", node)
            logging.info("Copying to %s", hostname)
            self.connect_ssh.scp_copy_to_node(hostname, str(pathlib.Path(self.args.config_dirpath, jobfile_name)), COPY_DIRECTORY)
        return 

    def write_fio_yamlfile(self, vol_size, volume_name, pvc_name, fio_jobfile, io_size):
        """Create yaml with fio's input params"""

        yaml.add_representer(collections.OrderedDict, self.dict_representer)
        yaml.add_constructor(self._mapping_tag, self.dict_constructor)

        app_orig_yaml_path = str(pathlib.Path(self.args.config_dirpath, APP_YAML_TEMPLATE))
        app_yaml_path = str(pathlib.Path(self.args.log_dirpath, APP_YAML_TEMPLATE.split(".")[0] + "-" + \
            volume_name + str(vol_size) +  ".yaml"))
        logging.info("app_yaml_path: %s", app_yaml_path)

        dicts = []
        fio_options = "--directory=%s --aux-path=%s --size=%s --numjobs %s" % (FIO_MOUNTPATH, FIO_MOUNTPATH, io_size/int(self.args.fio_num_jobs), self.args.fio_num_jobs)
        with open(app_orig_yaml_path, 'r') as stream, open(app_yaml_path, 'w') as outfile:
            dicts = list(yaml.load_all(stream))
            for yaml_dict in dicts:
                if yaml_dict['kind'] == 'Pod':
                    yaml_dict['metadata']['name'] = self.args.fio_name + str(vol_size)
                    yaml_dict['metadata']['labels']['name'] = self.args.fio_name + str(vol_size)
                    yaml_dict['spec']['containers'][0]['name'] = self.args.fio_name + str(vol_size)
                    yaml_dict['spec']['containers'][0]['image'] = self.args.fio_image
                    yaml_dict['spec']['containers'][0]['env'][0]['value'] = fio_options
                    yaml_dict['spec']['containers'][0]['env'][1]['value'] = fio_jobfile
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['name'] = FIO_MOUNTNAME
                    yaml_dict['spec']['containers'][0]['volumeMounts'][0]['mountPath'] = FIO_MOUNTPATH
                    yaml_dict['spec']['volumes'][0]['name'] = FIO_MOUNTNAME
                    yaml_dict['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = pvc_name
                    yaml_dict['spec']['volumes'][1]['hostPath']['path'] = COPY_DIRECTORY
                elif yaml_dict['kind'] == 'PersistentVolumeClaim':
                    # PVC changes
                    raise ValueError("Yaml template seems old/deprecated")
                else:
                    raise ValueError("Yaml template file: %s is unsupported" % app_orig_yaml_path)
            yaml.dump_all(dicts, outfile, default_flow_style=False)
        return app_yaml_path

    def check_pods(self, cluster_name):
        """Check fio is running fine in all pods"""

        # We should have 1 pod for each volume.
        num_pods_done = 0
        for i in self.args.list_volume_sizes:
            pod_name = self.args.fio_name + str(i)
            try:
                logging.info("------ Start checking Pod Name: %s ------", pod_name)
                self.kubectl_helper[cluster_name].check_pod_running(pod_name)

                # Check fio showsup in 'ps -ef' command inside the container
                cmd_ps = "ps -ef | grep fio"
                result = self.kubectl_helper[cluster_name].run_kubectl_exec_cmd(cmd_ps, pod_name, pod_name)
                logging.info(result)
                if not result or "fio" not in result:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s" % (cmd_ps, pod_name))
                logging.info("O/P of 'ps -ef' cmd shows fio is running in pod: %s", pod_name)

                # Show timestamp on fio o/p file
                cmd_ls = "ls -lrt " + FIO_MOUNTPATH
                result = self.kubectl_helper[cluster_name].run_kubectl_exec_cmd(cmd_ls, pod_name, pod_name)
                logging.info(result)
                if not result or len(result) < 1:
                    raise Exception("No output when cmd: %s "
                                    "was run in pod: %s. Result: %s" % (cmd_ls, pod_name, result))
                logging.info("ls -lrt of 'datadir' ran fine in pod: %s", pod_name)

                # Show container's logs
                result = self.kubectl_helper[cluster_name].run_kubectl_logs_cmd(pod_name, pod_name)
                logging.info(result)
                if not result or "error" in result:
                    raise Exception("No output OR found 'error' while collecting logs "
                                    "from pod: %s" % pod_name)
                logging.info("kubectl log shows fio stats - fine in pod: %s",
                             pod_name)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
            except subprocess.CalledProcessError as err:
                time.sleep(5)
                if not self.kubectl_helper[cluster_name].check_pod_completed(pod_name):
                    raise
                logging.info(err.output)
                logging.info("Pod name: %s has completed its run. Container's "
                             "log:", pod_name)
                result = self.kubectl_helper[cluster_name].run_kubectl_logs_cmd(pod_name, pod_name)
                logging.info(result)
                num_pods_done = num_pods_done + 1
                logging.info("num_pods_done: %d", num_pods_done)
                logging.info("------ Done checking Pod Name: %s ------", pod_name)
                if num_pods_done == len(self.args.list_volume_sizes):
                    return True
                else:
                    # check remaining pods
                    pass

    def run_volume_bind_publish(self):
        # Check for app clusters
        src = self.args.app_cluster1
        dest = self.args.app_cluster2
        clusters = {}
        if not self.args.nuvo_cluster1:
                self.nuvo_mgmt.kubectl_helper.context = src
                self.install_dependencies()
                self.args.nuvo_cluster1 = self.create_application_cluster(src)
                if src == dest:
                    self.args.nuvo_cluster2 = self.args.nuvo_cluster1
                clusters[src] = self.args.nuvo_cluster1
        else:
            logging.info("Cluster %s already created. Skipping app cluster creation", src)

        if not self.args.nuvo_cluster2:
                self.nuvo_mgmt.kubectl_helper.context = dest
                self.install_dependencies()
                self.args.nuvo_cluster2 = self.create_application_cluster(dest)
                clusters[dest] = self.args.nuvo_cluster2
        else:
            logging.info("Cluster %s already created. Skipping app cluster creation", dest)

        self.nuvo_mgmt.kubectl_helper.context = src
        spa_id = self.nuvo_mgmt.do_service_plan_allocation(self.args.nuvo_cluster1, self.args.account_name)
        self.nuvo_mgmt.launch_secret_yaml(self.args.nuvo_cluster1)
        # For every volume size, create a dynamically provisioned volume and launch fio.
        pvc_name = {}
        volume_details = {}
        for vol_size in self.args.list_volume_sizes:
            pod_name = self.args.fio_name + str(vol_size)
            pvc_name_prefix="nuvoauto-pvc-"
            pvc_name[pod_name] = pvc_name_prefix + str(uuid.uuid4())[:5]
            volume_name = self.nuvo_mgmt.launch_volume_pvc_dynamic(pvc_name[pod_name], spa_id, vol_size=vol_size)
            logging.info("volume_name: %s, pvc_name: %s", volume_name, pvc_name[pod_name])

            # create the app yaml file based off the template
            io_size = 0.8 * (int(vol_size) << 30)
            app_yaml_path = self.write_fio_yamlfile(vol_size, volume_name, pvc_name[pod_name], WRITE_JOB, io_size)
            self.copy_jobfile_to_nodes(WRITE_JOB, src)
            self.kubectl_helper[src].deploy_appcontainer(app_yaml_path, volume_name,
                    pod_name=pod_name, pvc_name=pvc_name[pod_name], dynamic=True)
            volume_details[volume_name] = int(vol_size)
            
        all_pods_done = False
        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            if self.check_pods(src):
                all_pods_done = True
                logging.info("All fio write pods have completed successfully.")
                break
            else:
                logging.info("Sleeping for %s before checking the pods again...",
                             self.args.fio_sleep_time)
                time.sleep(self.args.fio_sleep_time)

        if all_pods_done:
            logging.info("SUCCESS: All pods with fio ran fine.")
        else:
            raise RuntimeError("Some/all pods did not reach 'Completed' state within time limit")

        self.nuvo_mgmt.kubectl_helper.context = dest
        spa_id = self.nuvo_mgmt.do_service_plan_allocation(self.args.nuvo_cluster2, self.args.account_name)
        self.nuvo_mgmt.launch_secret_yaml(self.args.nuvo_cluster2)
        for pod in pvc_name.keys():
            self.kubectl_helper[src].delete_pod(pod)
            volume_name = json.loads(self.kubectl_helper[src].get_pvc(pvc_name=pvc_name[pod], output='json'))['spec']['volumeName']
            self.kubectl_helper[src].delete_pvc(pvc_name[pod])
            # publish to dest cluster
            self.nuvo_mgmt.bind_and_publish_volume(volume_name, self.args.nuvo_cluster2)

        for volume_name in volume_details.keys():
            vol_size = volume_details[volume_name]
            pod_name = self.args.fio_name + str(vol_size)
            pvc = self.nuvo_mgmt.launch_volume_pvc(volume_name)
            vol_id = self.nuvo_mgmt.get_volume_id(volume_name)
            logging.info("volume_name: %s, pvc_name: %s", volume_name, pvc)

            # create the app yaml file based off the template
            io_size = 0.8 * (int(vol_size) << 30)
            app_yaml_path = self.write_fio_yamlfile(vol_size, volume_name, pvc, READ_JOB, io_size)
            self.copy_jobfile_to_nodes(READ_JOB, dest)
            # Start read verify job
            self.kubectl_helper[dest].deploy_appcontainer(app_yaml_path, volume_name,
                    pod_name=pod_name, pvc_name=pvc, dynamic=False, vol_id=vol_id)

        # check for pods completion and exit

        all_pods_done = False
        time_now = datetime.datetime.utcnow()
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                seconds=int(self.args.fio_timeout) + TEST_RUNTIME_DELTA)):
            logging.info("========================")
            if self.check_pods(dest):
                all_pods_done = True
                logging.info("All fio write pods have completed successfully.")
                break
            else:
                logging.info("Sleeping for %s before checking the pods again...",
                             self.args.fio_sleep_time)
                time.sleep(self.args.fio_sleep_time)

        if all_pods_done:
            logging.info("SUCCESS: All pods with fio ran fine.")
        else:
            raise RuntimeError("Some/all pods did not reach 'Completed' state within time limit")
                
        # cleanup
        if self.args.do_cleanup:
            logging.info("Test succeeded. Deleting kops cluster now")
            KopsCluster.kops_delete_cluster(kops_cluster_name=self.args.app_cluster1,
                                            kops_state_store=self.args.kops_state_store)
            KopsCluster.kops_delete_cluster(kops_cluster_name=self.args.app_cluster2,
                                                kops_state_store=self.args.kops_state_store)
            domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.args.nuvo_cluster1)
            self.nuvo_mgmt.wait_for_cluster_state(self.args.nuvo_cluster1, "TIMED_OUT", domain_id=domain_id)
            self.kubectl_helper[src].cleanup_cluster(self.args.nuvo_cluster1, \
                self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'] , self.args.account_name)
            domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.args.nuvo_cluster2)
            self.nuvo_mgmt.wait_for_cluster_state(self.args.nuvo_cluster2, "TIMED_OUT", domain_id=domain_id)
            self.kubectl_helper[dest].cleanup_cluster(self.args.nuvo_cluster2, \
                self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'], self.args.account_name)
        else:
            for pod in pvc_name.keys():
                self.kubectl_helper[dest].delete_pod(pod)
            logging.info("Test succeeded. Pods deleted.")
            for k8s_name, nuvo_cluster_name in clusters.items():
                KopsCluster.kops_delete_cluster(kops_cluster_name=k8s_name,
                                                kops_state_store=self.args.kops_state_store)
                domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(nuvo_cluster_name)
                self.nuvo_mgmt.wait_for_cluster_state(nuvo_cluster_name, "TIMED_OUT", domain_id=domain_id)
                self.kubectl_helper[k8s_name].cleanup_cluster(nuvo_cluster_name, \
                    self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'], self.args.account_name)

def main():
    """main"""

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo data plane and runs fio against all volumes")
    parser.add_argument(
        '--app_cluster1', help='name of app cluster from which volume will be unbound and published')
    parser.add_argument(
        '--app_cluster2', help='name of app cluster to which volume will be unbound and published')
    parser.add_argument(
        '--nuvo_cluster1', help='name of cluster object, if created in the kontroller for app_cluster1')
    parser.add_argument(
        '--nuvo_cluster2', help='name of cluster object, if created in the kontroller for app_cluster2')
    parser.add_argument(
        '--nodes', help='number of nodes in the cluster [default=3]', type=int, default=3,
        choices=range(1, 101))
    parser.add_argument(
        '--kops_state_store', help='state store for both clusters')
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey')
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey')
    parser.add_argument(
        '--region', help='aws region', default='us-west-2')
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node',
        default='us-west-2c')
    parser.add_argument(
        '--k8s_nodes_zone', help='aws zone for other nodes ',
        default='us-west-2c')
    parser.add_argument(
        '--master_size', help='ec2 instance type for master node ', default='m5d.large')
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes ', default='m5d.large')
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--config_dirpath', help='directory path to user-app yaml files.'
        '[default=${HOME}/testing/config/')
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Demo Tenant')
    parser.add_argument(
        '--volume_sizes', help='Comma separated list of volume sizes', default="1,2")
    parser.add_argument(
        '--fio_image', help='Path to Docker image for fio container '
        'default: 407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3', 
        default='407798037446.dkr.ecr.us-west-2.amazonaws.com/nuvolosotest/fiotest:v3')
    parser.add_argument(
        '--fio_timeout', help='Time in seconds, in addtion to a delta of {0}s to wait for the fio job(s) to complete. \
        [DEFAULT=300] based on default volume sizes'.format(TEST_RUNTIME_DELTA), default=300)
    parser.add_argument(
        '--fio_sleep_time', help='Sleep in seconds [default=120] '
        'before checking the pods again', type=int, default=120)
    parser.add_argument(
        '--fio_name', help='Name for the fio pods', default="fiov")
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--do_cleanup', help='Delete both app clusters in the end',
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
        '--serviceplan_name', help='Name of service plan ', default='General')
    parser.add_argument(
        '--fio_num_jobs', help='Number of fio jobs. [Default: 1]', default=1)

    args = parser.parse_args()
    try:
        assert(args.nuvo_cluster1 and args.nuvo_cluster2 and args.app_cluster1 and args.app_cluster2 )
    except:
        assert(args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"

    home_dir = str(pathlib.Path.home())
    if not args.config_dirpath:
        args.config_dirpath = str(pathlib.Path(__file__).parent.absolute()/'../config/')

    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    args.list_volume_sizes = args.volume_sizes.strip().split(",")
    assert(args.list_volume_sizes), "'volume_sizes' param was empty"
    logging.info("len args.list_volume_sizes: %d", len(args.list_volume_sizes))
    logging.info("args.list_volume_sizes: %s", args.list_volume_sizes)
    logging.info("args.fio_timeout: %s", args.fio_timeout)
    args.kops_cluster_name = args.app_cluster1
    logging.info(args)

    print("Script to test volume bind-publish workflow")
    test = TestBindPublish(args)
    test.run_volume_bind_publish()


if __name__ == '__main__':
    main()
