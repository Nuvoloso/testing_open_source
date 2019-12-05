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

Deploys a KOPS cluster along with Nuvo Management. Upon a successful completion, it stores
the hostname of Nuvo Kontroller in ./nuvomgmthostname.txt

Dependencies:
It needs the nuvodeployment.yaml in the HOME directory of the host. Python3 is required for running
this script.
The script is currently supported on Ubuntu only (Recommended AMI: ami-702d7f08).


Example of a run command:
python3 deploy_nuvo_kontroller.py --kops_cluster_name nuvokontrollertestauto.k8s.local --nodes 1 \
--kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIAJXXXXMQ \
--aws_secret_access_key yISSSSSSSS6xbhCg/ --region us-west-2

After the run, get the Nuvo Management's hostname:
ubuntu@ip-172-31-47-138:~$ cat nuvomgmthostname.txt
ad16787c6ff3211e882030a9bfad664b-1609324992.us-west-2.elb.amazonaws.com

Note: Unlike other scripts (endtoend.py, fio_multivolumes.py) this is a standalone tool and doesn't
rely on library modules under lib/

Detailed steps:
1. Reads input params
2. Installs kops, kubectl, awscli
3. Configures aws. This step is skipped if '.aws/credentials' exists. Otherwise, it creates 'config'
and 'credentials' files under '.aws' directory.
4. Generates ssh-keypair unless '/.ssh/id_rsa' already exists
5. Runs 'kops create cluster' command without '--yes' option to check setup is valid.
Note: Unlike other tests, this tool has fixed values for the params of 'kops create' command. E.g.
--master-size t2.medium, --image=ami-ca89eeb2 etc. Also note, K8s version in this image is
Ubuntu 16.04.
6. Runs 'kops update cluster' (and 'kops update cluster', if needed) with '--yes' and loops/waits
till cluster is ready.
7. Deploys '-nuvoloso-management' cluster namespace using the nuvodeployment.yaml that is passed
as input param to this too. Note: You could change tags etc. in the yaml file to point to your
branch. The tool isn't sensitive to the contents of this file. It simply deploys the kubectl cluster
using it.
8. Waits till all three pods in '-n nuvoloso-management' namespace go to 'Running' state.
9. Exposes 'nuvo-https' service via a loadbalancer. This is done we can receive incoming traffic.
10. Gets the hostname of nuvoloso-management service.
Note: To access this newly deployed management plane (aka Nuvoloso controller), goto:
https://<hostname printed in step #9>
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import subprocess
import tempfile
import time
import uuid
from ruamel.yaml import YAML
from botocore.exceptions import ClientError

CHECK_OUTPUT_TIMEOUT = 360
DEFAULT_CLUSTER_NAME = "NuvoKontrollerTestAuto.k8s.local"
NUVO_DEPLOYMENT_YAML = "nuvodeployment.yaml"
NUVOMGMT_HOSTFILE = "nuvomgmthostname.txt"
# Namespace '-n nuvoloso-management' has 3 pods - configdb-xx, metricsdb-xx, services-xx
WAIT_TIMEOUT = 600

# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

# default region for clusters
DEFAULT_REGION = 'us-west-2'

# default zone
DEFAULT_ZONE = DEFAULT_REGION + 'c'

DEFAULT_NODE_SIZE = 't2.large'

class NuvoControlPlane(object):
    '''Deploys Nuvo Control Plane (aka 'Kontroller') inside a new KOPS cluster'''

    @staticmethod
    def get_home():
        '''Returns home dir of host'''
        return os.environ.get('HOME') if os.environ.get('HOME') else "~"

    @staticmethod
    def apt_get_update():
        '''Run apt-get update to upate the packages'''

        cmd_apt_get_update = "sudo apt-get update"
        try:
            result = subprocess.check_output(cmd_apt_get_update, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result: logging.info(result)
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise

    @staticmethod
    def install_kops():
        '''Install KOPS'''

        cmd_wget_kops = "wget -O kops https://github.com/kubernetes/kops/releases/download" \
            "/$(curl -s https://api.github.com/repos/kubernetes/kops/releases/latest | " \
            "grep tag_name | cut -d '\"' -f 4)/kops-linux-amd64"
        cmd_chmod_mv = "chmod +x ./kops ; sudo mv ./kops /usr/local/bin/"
        try:
            result = subprocess.check_output("which kops", stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and result.strip()[-4:] == "kops":
                logging.info(result)
                logging.info("kops is already installed")
            else:
                raise Exception("'which kops' output is unexpected")
                
        except subprocess.CalledProcessError as err:
            if not err.output:
                # which command yielded nothing. That means kops is not installed.
                result = subprocess.check_output(cmd_wget_kops, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
                result = subprocess.check_output(cmd_chmod_mv, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
                result = subprocess.check_output("which kops", stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and result.strip()[-4:] == "kops":
                    logging.info(result)
                else:
                    logging.info(result)
                    raise Exception("Failed to install kops")
            else:
                logging.info(err.output)
                raise

    @staticmethod
    def install_kubectl():
        '''Install Kubectl'''

        cmd_wget_kubectl = "curl -LO https://storage.googleapis.com/kubernetes-release/release/" \
            "v1.8.7/bin/linux/amd64/kubectl ; curl -LO https://storage.googleapis.com/" \
            "kubernetes-release/release/$(curl -s https://storage.googleapis.com/" \
            "kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl"
        cmd_chmod_mv = "chmod +x ./kubectl ; sudo mv ./kubectl /usr/local/bin/kubectl"
        try:
            result = subprocess.check_output("which kubectl", stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and result.strip()[-7:] == "kubectl":
                logging.info(result)
                logging.info("kubectl is already installed")
            else:
                raise Exception("'which kubectl' output is unexpected")
        except subprocess.CalledProcessError as err:
            if not err.output:
                result = subprocess.check_output(cmd_wget_kubectl, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
                result = subprocess.check_output(cmd_chmod_mv, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
                result = subprocess.check_output("which kubectl", stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and result.strip()[-7:] == "kubectl":
                    logging.info(result)
                else:
                    raise Exception("Failed to install kubectl")
            else:
                logging.info(err.output)
                raise

    @staticmethod
    def install_awscli():
        '''Install AWS CLI'''

        cmd_install_awscli = "sudo apt -y install awscli"
        cmd_check_version = "aws --version"
        try:
            result = subprocess.check_output(cmd_check_version, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and "not found" not in result and "botocore" in result:
                logging.info(result)
                logging.info("aws-cli is already installed")
            else:
                raise Exception("'aws --version' output is unexpected")
        except subprocess.CalledProcessError as err:
            if err.returncode == 127:
                if err.output: logging.info(err.output)
                result = subprocess.check_output(cmd_install_awscli, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
                result = subprocess.check_output(cmd_check_version, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and "botocore" in result:
                    logging.info(result)
                else:
                    raise Exception("Failed to install awscli")
            else:
                raise

    @staticmethod
    def configure_aws(args):
        '''Configure AWS'''

        cmd_mkdir = "mkdir " + NuvoControlPlane.get_home() + "/.aws"
        cmd_touch_config = "touch " + NuvoControlPlane.get_home() + "/.aws/config"
        cmd_touch_credentials = "touch " + NuvoControlPlane.get_home() + "/.aws/credentials"

        cmd_access_key = "aws configure set aws_access_key_id " + args.aws_access_key
        cmd_secret_access_key = "aws configure set aws_secret_access_key " + \
            args.aws_secret_access_key
        cmd_set_aws_region = "aws configure set default.region " + args.region
        try:
            result = subprocess.check_output(cmd_touch_credentials, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info("aws credentials file (.aws/credentials) exists. Skipping aws configure. "
                         "%s", result)
        except subprocess.CalledProcessError as err:
            if not err.output or "No such file or directory" not in err.output:
                raise
            logging.info(err.output)
            try:
                result = subprocess.check_output(cmd_mkdir, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_touch_config, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_touch_credentials, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_access_key, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_secret_access_key, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_set_aws_region, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                #TODO - verify files created by above cmds
            except subprocess.CalledProcessError as err:
                if err.output: logging.info(err.output)
                logging.info("Failed to configure aws.")
                raise

    @staticmethod
    def generate_sshkeypair():
        '''ssh-keygen -t rsa'''

        cmd_generate_sshkeypair = "yes " + NuvoControlPlane.get_home() + \
            "/.ssh/id_rsa |ssh-keygen -q -t rsa -N '' >/dev/null"
        try:
            if os.path.isfile(NuvoControlPlane.get_home() +"/.ssh/id_rsa"):
                logging.info("ssh keypair seems to exist already. We'll use it instead")
                return
            else:
                logging.info("Generating ssh key pair now")
                result = subprocess.check_output(cmd_generate_sshkeypair, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logging.info(result)
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise

    @staticmethod
    def kops_cluster_validate(args):
        '''Validate KOPS cluster'''

        kops_cluster_validate = "kops validate cluster --name " + args.kops_cluster_name + \
            " --state " + args.kops_state_store
        success_str = "Your cluster " + args.kops_cluster_name + " is ready"
        response = False
        try:
            logging.info("Running kops cmd: %s", kops_cluster_validate)
            result = subprocess.check_output([kops_cluster_validate], stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result:
                logging.info(result)
                response = True if success_str in result else False
            return response
        except subprocess.CalledProcessError as err:
            if err.output:
                logging.info(err.output)
            return False

    @staticmethod
    def kops_cluster_update(args):
        '''KOPS cluster update'''

        kops_cluster_update = "kops update cluster --name " + args.kops_cluster_name + \
            " --state " + args.kops_state_store + " --yes"
        kops_rolling_update = "kops rolling-update cluster --name " + args.kops_cluster_name + \
            " --state " + args.kops_state_store + " --yes"
        try:
            logging.info("Running kops cmd: %s", kops_cluster_update)
            result = subprocess.check_output([kops_cluster_update], stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if not result:
                raise Exception("kops update cluster yielded no output")
            logging.info(result)
            if "already exists" in result:
                raise Exception("Nuvo cluster ALREADY exists. Delete that first?")
            elif "kops rolling-update cluster" in result:
                logging.info("Running kops cmd: %s", kops_rolling_update)
                result = subprocess.check_output([kops_rolling_update], stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                logging.info(result)
                logging.info("count: %s", str(result.count("Ready")))
                if result.count("Ready") == args.nodes + 1:
                    logging.info("All nodes in kops clustter are ready")
                else:
                    #TODO
                    pass
            elif "Cluster is starting.  It should be ready in a few minutes" in result:
                time_now = datetime.datetime.utcnow()
                cmd_succeeded = False
                while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                        seconds=WAIT_TIMEOUT)):
                    time.sleep(60)
                    success = NuvoControlPlane.kops_cluster_validate(args)
                    if success:
                        cmd_succeeded = True
                        break
                    else:
                        logging.info("Cluster is not ready yet. Sleeping for 60 seconds.")
                if not cmd_succeeded:
                    raise Exception("TIMEOUT (%s seconds) waiting for kops cluster %s to go "
                                    "to 'ready' state." % (WAIT_TIMEOUT, args.kops_cluster_name))
        except subprocess.CalledProcessError as err:
            if err.output:
                print(err.output)
                logging.info(err.output)
            logging.info("kops update cluster failed. Running - validate cluster now")
            output = NuvoControlPlane.kops_cluster_validate(args)
            if output:
                print(output)
                logging.info(output)
            raise

    @staticmethod
    def create_kops_app_cluster(args):
        '''Create KOPS cluster'''

        cmd_kops_cluster_create = "kops create cluster --channel alpha --node-count " + \
            str(args.nodes) + " --image=" + DEFAULT_AMI + " --master-zones "+ DEFAULT_ZONE + " " \
                "--zones "+ DEFAULT_ZONE + " --node-size t2.medium --master-size "+ args.node_size + " " \
                    "--ssh-public-key " + NuvoControlPlane.get_home() + "/.ssh/id_rsa.pub " \
                        "--kubernetes-version " + args.kubernetes_version + " " + \
                            "--state " + args.kops_state_store + " --cloud-labels " \
                                " \"Team=Nuvoloso," + "Owner=TestAuto\" " + " --name " + \
                                    args.kops_cluster_name
        logging.info("Running kops cmd: %s", cmd_kops_cluster_create)
        try:
            logging.info("Creating kops cluster %s", args.kops_cluster_name)
            result = subprocess.check_output([cmd_kops_cluster_create], stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result:
                logging.info(result)
                if "Finally configure your cluster with" not in result:
                    raise Exception("Did not find 'Finally configure your cluster with' "
                                    "in output of 'kops create cluster' cmd")
            else:
                raise Exception("No output from 'kops create cluster' cmd. Something went wrong??")
        except subprocess.CalledProcessError as err:
            if not err.output or "already exists" not in err.output:
                if err.output:
                    print(err.output)
                    logging.info(err.output)
                logging.info("kops create cluster failed. Running - validate cluster now")
                output = NuvoControlPlane.kops_cluster_validate(args)
                if output:
                    print(output)
                    logging.info(output)
                raise
            print(err.output)
            logging.info(err.output)

    @staticmethod
    def kops_cluster_edit_csi(args):
        """Edit the cluster configuration to enable MountPropagation. MountPropagation
        is needed by the nuvo container so its FUSE mount head will be visible on
        the host.
        """
        cluster_name = args.kops_cluster_name
        state_store = args.kops_state_store

        cmd_list = [
            'kops', 'get', 'cluster', '--name=%s' % cluster_name, '--state=%s' % state_store,
            '-o', 'yaml'
        ]

        print(cmd_list)

        retcode = 0
        data = ''
        try:
            data = subprocess.check_output(cmd_list)
        except subprocess.CalledProcessError as exc:
            retcode = exc.returncode
            data = exc.output
            if data:
                print(data)

        if retcode != 0:
            return retcode

        yaml = YAML()
        try:
            cluster = yaml.load(data)
            if 'spec' not in cluster:
                raise ValueError
        except ValueError as exc:
            print('%s did not output valid yaml' % cmd_list)
            print(data)
            return 1

        cluster['spec']['kubeAPIServer'] = {
            'allowPrivileged': True
        }
        cluster['spec']['kubelet'] = {
            'allowPrivileged': True,
            'anonymousAuth': False,
            'featureGates': {
                'ExperimentalCriticalPodAnnotation': 'true'
            }
        }

        if args.bionic:
            # resolvConf is required to work around the bug that kubernetes does not pick up
            # the correct resolv.conf when systemd-resolved is used.
            # See https://github.com/kubernetes/kubeadm/issues/273
            # While this is fixed in kubeadm, kops does not use kubeadm.
            # Kops does not have a suitable built-in solution for this problem.
            resolv_conf = {
                'resolvConf': '/run/systemd/resolve/resolv.conf'
            }
            cluster['spec']['kubelet'].update(resolv_conf)

        if (args.container_log_max_size):
            print("Container log max size:  " + str(args.container_log_max_size) + "m")
            print("Container log max files: " + str(args.container_log_max_files))

            cluster['spec']['docker'] = {
                'logOpt': [
                    "max-size=" + str(args.container_log_max_size) + "m",
                    "max-file=" +  str(args.container_log_max_files)
                ]
            }

        data = yaml.dump(cluster)

        print(data)

        tmpname = ''
        with tempfile.NamedTemporaryFile(mode='w', prefix='cluster', suffix='.yaml', delete=False) as myfile:
            tmpname = myfile.name
            myfile.write(data)

        cmd_list = ['kops', 'replace', '-f', tmpname, '--state=%s' % state_store]
        print(cmd_list)

        retcode = subprocess.call(cmd_list)
        print('kops execution code', retcode)

        if retcode == 0:
            os.remove(tmpname)
        else:
            print('preserved temp file', tmpname)
        return retcode

    @staticmethod
    def get_total_pods(yaml_path):
        '''Check for number of mongo replicas and set the total number of pods accordingly'''

        yaml = YAML()
        fd = open(yaml_path, 'r')
        data = yaml.load_all(fd)

        pods = 0
        for doc in data:
            if doc['kind'] == "StatefulSet":
                pods += doc['spec']['replicas']

        fd.close()
        return pods

    @staticmethod
    def deploy_nuvo_kontroller(args):
        '''Deploy Nuvoloso Management Plane'''

        deploy_kontroller_cmd = "kubectl create -f "  + args.path_to_nuvodeployment_yaml
        get_pods_nuvo_mgmtcluster = "kubectl -n nuvoloso-management get pods"
        try:
            if not os.path.isfile(args.path_to_nuvodeployment_yaml):
                raise Exception("File: " + args.path_to_nuvodeployment_yaml +
                                " NOT found. Aborting..")
            result = subprocess.check_output(deploy_kontroller_cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info(result)
            time_now = datetime.datetime.utcnow()
            cmd_succeeded = False
            total_pods = NuvoControlPlane.get_total_pods(args.path_to_nuvodeployment_yaml)
            while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                    seconds=WAIT_TIMEOUT)):
                time.sleep(60)
                result = subprocess.check_output(get_pods_nuvo_mgmtcluster,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result:
                    logging.info(result)
                    #TODO: Parse each line to check for no-restarts and running.
                    if result.count("Running") == total_pods:
                        logging.info("SUCCESS: All pods in nuvo management "
                                     "cluster are running fine")
                        cmd_succeeded = True
                        break
                    else:
                        logging.info("Nuvo management cluster is not ready yet. "
                                     "Sleeping for 60 seconds..")
                else:
                    raise Exception("Failed during Nuvo management cluster. Command -' %s ' "
                                    " did not yield any response" % get_pods_nuvo_mgmtcluster)
            if not cmd_succeeded:
                raise Exception("TIMEOUT (%s seconds) waiting for nuvoloso-management cluster to"
                                "  go to 'Running' state." % WAIT_TIMEOUT)
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            cmd_kubectl_describe = "kubectl describe pods --namespace nuvoloso-management"
            output = subprocess.check_output(cmd_kubectl_describe, timeout=CHECK_OUTPUT_TIMEOUT,
                                             encoding='UTF-8', shell=True)
            if output: logging.info(output)
            raise

    @staticmethod
    def expose_deploy_services():
        '''Expose https service'''

        expose_deploy_services_cmd = "kubectl expose deploy services --type LoadBalancer " \
            "--port 443 --target-port 443 --name nuvo-https --namespace nuvoloso-management"
        try:
            result = subprocess.check_output(expose_deploy_services_cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info(result)
            if "service/nuvo-https exposed" in result:
                logging.info("nuvo mgmt service plane exposed successfully. Sleeping for 60"
                             " seconds for AWS LoadBalancer to start")
                time.sleep(60)
            else:
                raise Exception("Did NOT find 'service/nuvo-https exposed' in response")
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise

    @staticmethod
    def get_svc():
        '''Kubectl get service and write it to an output file'''

        get_svc_cmd = "kubectl get svc nuvo-https --namespace nuvoloso-management " \
            "-o yaml | grep hostname"
        try:
            result = subprocess.check_output(get_svc_cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info(result)
            print("NUVO Management: %s" % result)
            if "amazon" in result:
                logging.info("Management hostname was found %s", result)
                with open(NUVOMGMT_HOSTFILE, 'w') as f_write:
                    tmp_list = result.split(":")
                    f_write.write(tmp_list[1].strip())
                f_write.close()
            else:
                raise Exception("Did NOT find hostname for Nuvo Management")
        except  subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise

    @staticmethod
    def print_start_summary(args):
        '''Prints a summary of arguments'''

        logging.info("Starting operations with these parameters:")
        logging.info("===========================================")
        logging.info("Cluster: %s", args.kops_cluster_name)
        logging.info("Kops State store: %s", args.kops_state_store)
        logging.info("Node count: %s", args.nodes)
        logging.info("region: %s", args.region)
        logging.info("region: %s", args.path_to_nuvodeployment_yaml)
        logging.info("===========================================")

    @staticmethod
    def deploy(args):
        '''Invokes all methods in a set sequence'''

        NuvoControlPlane.print_start_summary(args)
        NuvoControlPlane.apt_get_update()
        NuvoControlPlane.install_kops()
        NuvoControlPlane.install_kubectl()
        NuvoControlPlane.install_awscli()
        NuvoControlPlane.configure_aws(args)
        NuvoControlPlane.generate_sshkeypair()
        NuvoControlPlane.create_kops_app_cluster(args)
        NuvoControlPlane.kops_cluster_edit_csi(args)
        NuvoControlPlane.kops_cluster_update(args)
        if not args.no_deploy:
            NuvoControlPlane.deploy_nuvo_kontroller(args)
            NuvoControlPlane.get_svc()

    @staticmethod
    def check_for_ubuntu_bionic(args):
        """Ubuntu bionic 18.04 requires some specific settings in the cluster config.
        Check the AMI requested in the args to see if it looks like a bionic AMI and set
        args.bionic = True if so. See edit_kops_cluster() for how this is used.

        The args.image can be specified either as "ami-*" or location, "owner/path".
        If the former, look up the AMI to find its location. Then check the location
        to see if it is "ubuntu-bionic".
        """
        image = args.image
        if image.startswith('ami-'):
            try:
                client = boto3.client('ec2', region_name=DEFAULT_REGION)
                response = client.describe_images(
                    ImageIds=[image]
                )
                if 'Images' not in response or len(response['Images']) != 1 or \
                        'ImageLocation' not in response['Images'][0]:
                    print('Unexpected response for describe_images:', response)
                    return 1
                image = response['Images'][0]['ImageLocation']
            except ClientError as exc:
                print('Unexpected error: %s' % exc)
                return 1

        if '-arm64-' in image:
            print ('Wrong architecture:', image)
            print ('Only amd64 images can be used with kops')
            return 1

        args.bionic = False
        if '/ubuntu-bionic-18.04' in image:
            print('Ubuntu Bionic 18.04 detected')
            print('IMPORTANT: All instance groups must use Ubuntu Bionic 18.04!')
            args.bionic = True

        return 0

def main():
    '''main'''

    default_cluster_name = DEFAULT_CLUSTER_NAME
    default_aws_access_key = default_aws_secret_access_key = default_kops_state_store = None

    if os.environ.get('KOPS_CLUSTER_NAME'):
        default_cluster_name = os.environ.get('KOPS_CLUSTER_NAME')

    if os.environ.get('KOPS_STATE_STORE'):
        default_kops_state_store = os.environ.get('KOPS_STATE_STORE')

    if os.environ.get('AWS_ACCESS_KEY'):
        default_aws_access_key = os.environ.get('AWS_ACCESS_KEY')

    if os.environ.get('AWS_SECRET_ACCESS_KEY'):
        default_aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')

    parser = argparse.ArgumentParser(description="Deploys a kops cluster with "
                                     "Nuvo Management Plane")
    parser.add_argument(
        '--kops_cluster_name', help='name of kops cluster [default: ' + default_cluster_name + ']',
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
        '--path_to_nuvodeployment_yaml', help='path to nuvodeployment.yaml '
        'file [default=${HOME}/nuvodeployment.yaml]')
    parser.add_argument(
        '--kubernetes_version', help='version of kubernetes to deploy', default='1.14.8')
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--container_log_max_size', help='container log max size', default=100)
    parser.add_argument(
        '--container_log_max_files', help='container log max files', default=3)
    parser.add_argument(
        '--no_deploy', action='store_true', help='do not execute a deploy operation')
    parser.add_argument(
        '--image', help='AMI identifier for all instances', default=DEFAULT_AMI)
    parser.add_argument(
        '--node_size', help='ec2 instance type for other nodes [t2.large]', default=DEFAULT_NODE_SIZE)

    home_dir = os.environ.get('HOME') if os.environ.get('HOME') else "~"
    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"
    if not args.no_deploy:
        if not args.path_to_nuvodeployment_yaml:
            args.path_to_nuvodeployment_yaml = home_dir + "/" + NUVO_DEPLOYMENT_YAML
        assert(os.path.isfile(args.path_to_nuvodeployment_yaml)), "File: " + \
            args.path_to_nuvodeployment_yaml + " NOT found. Aborting.."
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5]
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)

    if NuvoControlPlane.check_for_ubuntu_bionic(args) != 0:
        # check_for_ubuntu_bionic prints its own error message
        sys.exit(1)

    # deploy
    print("Script to deploy Kops cluster and namespace for Nuvo Kontroller")
    NuvoControlPlane.deploy(args)


if __name__ == '__main__':
    main()
