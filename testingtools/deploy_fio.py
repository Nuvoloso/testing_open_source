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
python3 testing/scripts/deploy_fio.py --kops_cluster_name nuvodataplanecluster.k8s.local \
 --nodes 2 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIAJXXXXMQ \
--aws_secret_access_key yISSSSSSSS6xbhCg/ --region us-west-2 --nuvo_kontroller_hostname \
ac7862895022711e9b5fe0acfcc322df-2138322790.us-west-2.elb.amazonaws.com

Example with non-default aws zone/instance type:
python3 testing/testingtools/deploy_fio.py --kops_cluster_name nuvodata.k8s.local  \
--nodes 2 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIAXXXXKA \
--aws_secret_access_key 1vdDXXXXDU+A --region us-west-2  \
--k8s_master_zone us-west-2a --k8s_nodes_zone us-west-2a --node_size t2.micro  \
--nuvo_kontroller_hostname aa97a3888349211e9b13c0a8c7db7864-1977142800.us-west-2.elb.amazonaws.com

It exits after successfully launching FIO app.
"""

import argparse
import datetime
import logging
import os
import time
import subprocess
import uuid


from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.api.nuvo_management import NuvoManagement

CHECK_OUTPUT_TIMEOUT = 600
DEFAULT_CENTRALD_CLUSTER_NAME = "NuvoCentraldTestAuto.k8s.local"
USERAPP_FILENAME = "fio.yaml"
WAIT_TIMEOUT = 600

# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

logging.basicConfig(format='%(asctime)s %(message)s', filename=__file__ + ".log",
                    level=logging.INFO)


class TestRunFio(object):
    '''Deploys Fio app container that runs IO against NUVO volume'''

    def __init__(self, args):
        self.args = args
        self.get_pv = "kubectl get pv"
        self.get_pods_nuvo_cluster = "kubectl get pods -n nuvoloso-cluster"
        self.nuvo_mgmt = NuvoManagement(args)

    def deploy_appcontainer(self, volume_name, nuvomatcher_id, vol_size):
        '''Deploys a container for user-app (fio) with Nuvo volume'''

        # Edit the yaml file to add nuvomatcher id
        # Copy original app yaml file into a new one with nuvo-matcher string.
        app_orig_yaml_path = self.args.config_dirpath + USERAPP_FILENAME
        app_yaml_path = self.args.config_dirpath + USERAPP_FILENAME.split(".")[0] + "-" + \
            str(uuid.uuid4())[:5] + ".yaml"
        with open(app_orig_yaml_path, "r") as f_read, open(app_yaml_path, 'w') as f_write:
            lines = (f_read.read()).rstrip()
            lines = lines + " " + nuvomatcher_id
            f_write.write(lines)
            f_write.write("\n")
        f_write.close()
        f_read.close()

        logging.info("Deploy user-app yaml file: %s to create a "
                     "container using nuvo-vol volume: %s ", app_yaml_path, volume_name)
        get_pods_app_cluster = "kubectl get pods"
        describe_pods_app_cluster = "kubectl describe pods"
        try:
            result = subprocess.check_output("kubectl create -f " + app_yaml_path,
                                             stderr=subprocess.STDOUT, timeout=CHECK_OUTPUT_TIMEOUT,
                                             encoding='UTF-8', shell=True)
            if result: logging.info(result)
            if self._wait_for_app_container(count=1):
                result = subprocess.check_output(self.get_pv, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT,
                                                 encoding='UTF-8', shell=True)
                if result: logging.info(result)
                if self.nuvo_mgmt.get_volume_id(
                        volume_name) in result and str(vol_size) in result and (result.count(
                            "Bound") == 1):
                    logging.info("SUCCESS - nuvo-volume with matcher_ud: %s mounted fine "
                                 " inside app container", nuvomatcher_id)
                else:
                    raise Exception("nuvo-volume's matcher id not found in result")
            else:
                raise Exception("App container Failed to deploy")
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            result = subprocess.check_output(describe_pods_app_cluster, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result: logging.info(result)
            result = subprocess.check_output(self.get_pods_nuvo_cluster, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result: logging.info(result)
            result = subprocess.check_output(self.get_pv, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result: logging.info(result)
            logging.info("Failed to deploy app cluster/namespace")
            raise

    def _wait_for_app_container(self, count=1):
        time_now = datetime.datetime.utcnow()
        cmd_succeeded = False
        get_pods_app_cluster = "kubectl get pods"
        describe_pods_app_cluster = "kubectl describe pods"
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            time.sleep(60)
            result = subprocess.check_output(get_pods_app_cluster, stderr=subprocess.STDOUT,
                                         timeout=CHECK_OUTPUT_TIMEOUT,
                                         encoding='UTF-8', shell=True)
            if result:
                logging.info(result)
                #TODO: Parse each line to check for no-restarts and running.
                if result.count("Running") == count:
                    logging.info("Pod in app cluster ('default') is running fine")
                    cmd_succeeded = True
                    time.sleep(20)
                    break
                else:
                    logging.info("App cluster (default namespace) is not ready yet. "
                                "Sleeping for 60 seconds..")
            else:
                raise Exception("Failed while deploying App cluster. Command -' %s ' "
                                " did not yield any response" % get_pods_app_cluster)
        if not cmd_succeeded:
                raise Exception("TIMEOUT (%s seconds) waiting for app cluster to"
                                "  go to 'Running' state." % WAIT_TIMEOUT)
        return cmd_succeeded

    def deploy_fio(self):
        '''Run fio app - long running test'''

        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        KopsCluster.create_kops_app_cluster(self.args)

        csp_name = self.nuvo_mgmt.create_csp_domain()
        # Comment below line for short run
        nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_name)
        self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name)
        volume_name = self.nuvo_mgmt.create_volume(nuvo_cluster_name,
                                                   vol_size=self.args.vol_size)
        # launch volume in k8s
        nuvomatcher_id = self.nuvo_mgmt.launch_volume(volume_name, self.args.vol_size)

        # fio
        self.deploy_appcontainer(volume_name, nuvomatcher_id, self.args.vol_size)

def main():
    '''main'''

    default_cluster_name = DEFAULT_CENTRALD_CLUSTER_NAME
    default_aws_access_key = default_aws_secret_access_key = default_kops_state_store = None

    if os.environ.get('KOPS_CLUSTER_NAME'):
        default_cluster_name = os.environ.get('KOPS_CLUSTER_NAME')

    if os.environ.get('KOPS_STATE_STORE') is None:
        default_kops_state_store = os.environ.get('KOPS_STATE_STORE')

    if os.environ.get('AWS_ACCESS_KEY'):
        default_aws_access_key = os.environ.get('AWS_ACCESS_KEY')

    if os.environ.get('AWS_SECRET_ACCESS_KEY') is None:
        default_aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo data plane")
    parser.add_argument(
        '--kops_cluster_name', help='name of kops cluster for Nuvo Data Plane[default: ' +
        default_cluster_name + ']',
        default=default_cluster_name)
    parser.add_argument(
        '--nodes', help='number of nodes in the cluster [default=1]', type=int, default=1,
        choices=range(1, 5))
    parser.add_argument(
        '--kops_state_store', help='state store for cluster',
        default=default_kops_state_store)
    parser.add_argument(
        '--aws_access_key', help='aws AccessKey', default=default_aws_access_key)
    parser.add_argument(
        '--aws_secret_access_key', help='aws SecretAccessKey',
        default=default_aws_secret_access_key)
    parser.add_argument(
        '--region', help='aws region [default=us-west-2]', default='us-west-2')
    parser.add_argument(
        '--k8s_master_zone', help='aws zone for master node [default=us-west-2c]', default='us-west-2c')
    parser.add_argument(
        '--k8s_nodes_zone', help='aws zone for other nodes [default=us-west-2c]', default='us-west-2c')
    parser.add_argument(
        '--master_size', help='ec2 instance type for master node [t2.large]', default='t2.large')
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes [t2.large]', default='t2.large')
    parser.add_argument(
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller',
        required=True)
    parser.add_argument(
        '--config_dirpath', help='directory path to user-app yaml files.'
        '[default=${HOME}/testing/config/')
    parser.add_argument(
        '--vol_size', help='Volume size in GiB [default=5]', type=int, default=5,
        choices=range(1, 10))
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Demo Tenant')
    parser.add_argument(
        '--image', help='AMI Image for all instances', default=DEFAULT_AMI)

    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"
    if not args.config_dirpath:
        home_dir = os.environ.get('HOME') if os.environ.get('HOME') else "~"
        args.config_dirpath = home_dir + "/testing/config/"

    print("Script to Run FIO against a NUVO volume - Long Running test")
    test = TestRunFio(args)
    test.deploy_fio()


if __name__ == '__main__':
    main()
