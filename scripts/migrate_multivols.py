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

Deploys an application container which copies data into volumes.
Data is read from a tar file and written into Nuvo volume that is mounted on "/datadir".

Dependencies:
It needs the Nuvo Kontroller hostname as input. To deploy one, use deploy_nuvo_kontroller.py
PYTHONPATH needs to be set in the environment (export PYTHONPATH=/home/ubuntu/testing/lib)


Example of a run command:
 python3 testing/scripts/migrate_multivols.py --kops_cluster_name nuvodata.k8s.local  --nodes 2 \
 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIXXKA  \
 --aws_secret_access_key 1vdXXX+A --region us-west-2 \
 --nuvo_kontroller_hostname a22b607.us-west-2.elb.amazonaws.com  --volume_sizes 20,30,40  \
 --volume_prefixes twenty,thirty,forty --tarfiles data20.tar,data30.tar,data40.tar --bucket myAWSbucket \
 --time_limit 3600  --pod_poll_freq 120

It exits after successfully copying data or the time limit.
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


from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement

CHECK_OUTPUT_TIMEOUT = 600
DEFAULT_CENTRALD_CLUSTER_NAME = "nuvotestcopymulti.k8s.local"
COPY_MOUNTPATH = "/datadir"
COPY_MOUNTNAME = "datadir"
TEST_RUNTIME_DELTA = 3600
NODE_LABEL = "copynode"
USERAPP_FILENAME = "migrate.yaml"
WAIT_TIMEOUT = 1200
VOLUME_CLAIM_SUFFIX = "c"

# use bionic version of ubuntu
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

class CopyTool(object):
    '''Deploys Fio app container that runs IO against NUVO volume'''

    def __init__(self, args):
        self.args = args
        self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args)
        self._mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG

    def dict_representer(self, dumper, data):
        """Yaml representer"""

        return dumper.represent_mapping(self._mapping_tag, data.items())

    def dict_constructor(self, loader, node):
        """Yaml constructor"""

        return collections.OrderedDict(loader.construct_pairs(node))

    def write_copy_yamlfile(self, iteration_number, volume_name, pvc_name, node_label):
        '''Create yaml with copy's input params'''

        myname = self.args.copy_name + '-' + str(iteration_number)
        logging.info("myname: %s", myname)

        yaml_data = {
          'apiVersion': 'v1',
          'kind': 'Pod',
          'metadata': {
            'name': myname,
            'labels': {
              'name': myname
            }
          },
          'spec': {
            'containers': [
              {
                'resources': None,
                'name': myname,
                'image': self.args.copy_image,
                'env': [
                  { 'name': 'AWS_ACCESS_KEY_ID', 'value': self.args.aws_access_key },
                  { 'name': 'AWS_SECRET_ACCESS_KEY', 'value': self.args.aws_secret_access_key },
                  { 'name': 'AWS_REGION', 'value': self.args.region },
                  { 'name': 'PS_VOLUME_NAME', 'value': 'PS1' },
                  { 'name': 'SRC_TYPE', 'value': 'S3' },
                  { 'name': 'OBJECT_NAME', 'value': self.args.list_volume_tarfiles[iteration_number] },
                  { 'name': 'SRC_BUCKET', 'value': self.args.bucket_name },
                  { 'name': 'SHOULD_WAIT', 'value': self.args.should_wait }
                ] ,
                'volumeMounts': [
                  {
                    'mountPath': COPY_MOUNTPATH,
                    'name': COPY_MOUNTNAME + str(iteration_number)
                  }
                ]
              }
            ],
            'nodeSelector': {
                'nodekey': node_label
            },
            'volumes': [
              {
                'name': COPY_MOUNTNAME + str(iteration_number),
                'persistentVolumeClaim': { 'claimName': pvc_name }
              }
            ],
            'restartPolicy': 'Never'
          }
        }

        app_yaml_path = self.args.log_dirpath + USERAPP_FILENAME.split(".")[0] + "-" + \
            volume_name + str(iteration_number) +  ".yaml"
        logging.info("app_yaml_path: %s", app_yaml_path)

        with open(app_yaml_path, 'w') as outfile:
            yaml.dump_all([yaml_data], outfile, default_flow_style=False)

        return app_yaml_path

    def check_pods(self):
        """Check copy is running fine in all pods"""

        # We should have 1 pod for each volume.
        num_pods_done = 0
        num_pods = len(self.args.list_volume_sizes)
        for i in range(0, num_pods):
            pod_name = self.args.copy_name + '-' + str(i)
            if self.kubectl_helper.check_pod_completed(pod_name):
                num_pods_done = num_pods_done + 1

        if num_pods_done == num_pods:
            for i in range(0, num_pods):
                result = self.kubectl_helper.run_kubectl_logs_cmd(pod_name, pod_name)
                logging.info(result)
            return True
        else:
            return False

    def label_all_nodes(self):
        """Label each of the nodes in the cluster with a unique string"""

        dict_label_node = {}
        node_list = self.kubectl_helper.get_nodes_cluster(self.args.kops_cluster_name)
        for i in range(len(node_list)):
            self.kubectl_helper.label_nodes(node_name=node_list[i], label_value=NODE_LABEL + str(i))
            # e.g. 'fionode0': 'ip-172-20-39-181.us-west-2.compute.internal'
            dict_label_node[NODE_LABEL + str(i)] = node_list[i]
        return dict_label_node

# Here is where the work happens

    def run_copy_multiple_vols(self):
        """Run copy app with multiple containers/volumes - long running test"""

        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        KopsCluster.create_kops_app_cluster(self.args)

        app_yaml_paths = []
        try:
            csp_name = self.nuvo_mgmt.create_csp_domain()
            nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_name)
            logging.info("nuvo_cluster_name: %s", nuvo_cluster_name)
            self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name)
            self.nuvo_mgmt.launch_secret_yaml(nuvo_cluster_name)

            # Label the nodes in the cluster
            dict_label_node = self.label_all_nodes()
            if not dict_label_node:
                raise Exception("dict_label_node is empty. Failed to label the nodes..")
            logging.info("dict_label_node: %s", dict_label_node)
            # we will read the node_label_list in round robin fashion
            nodes_circular_list = itertools.cycle(dict_label_node.keys())
            for i in range(0, len(self.args.list_volume_sizes)):
                # Create volumes
                logging.info("Iteration number: %d, Creating volume Size: "
                        "%s Prefix: %s ...", i, self.args.list_volume_sizes[i],
                        self.args.list_volume_prefixes[i])
                volume_name = self.nuvo_mgmt.create_volume(nuvo_cluster_name,
                                                           vol_size=self.args.list_volume_sizes[i],
                                                           vol_name_prefix=self.args.list_volume_prefixes[i])
                logging.info("volume_name: %s", volume_name)

                # launch volume in k8s
                pvc_name = self.nuvo_mgmt.launch_volume_pvc(volume_name)

                # create the app yaml file based off template
                curr_label = next(nodes_circular_list)
                app_yaml_path = self.write_copy_yamlfile(i, volume_name, pvc_name, node_label=curr_label)
                app_yaml_paths.append(app_yaml_path)
                pod_name=self.args.copy_name + '-' + str(i)
                self.kubectl_helper.deploy_appcontainer(app_yaml_path, volume_name,
                    pod_name=pod_name, pvc_name=pvc_name)
                if not self.kubectl_helper.check_pod_node_assignment(pod_name,
                                                                     dict_label_node[curr_label]):
                    raise Exception("pod_name: %s was NOT deployed on node_name: %s", pod_name,
                                    dict_label_node[curr_label])

            # Now we wait/poll to make sure copies are running successfully
            all_pods_done = False

            time_end = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.args.time_limit)
            while datetime.datetime.utcnow() <= time_end:
                logging.info("========================")
                if self.check_pods():
                    all_pods_done = True
                    logging.info("All pods have completed successfully.")
                    break
                else:
                    logging.info("Sleeping for %s before checking the pods again...",
                                 self.args.pod_poll_freq)
                    time.sleep(self.args.pod_poll_freq)

            if all_pods_done:
                logging.info("SUCCESS: All copy pods completed.")
            else:
                raise Exception("Some/all pods did not reach 'Completed' state within time limit")
            # cleanup
            logging.info("Test succeeded. Deleting kops cluster now")

            num_pods = len(self.args.list_volume_sizes)
            for i in range(0, num_pods):
                self.kubectl_helper.run_kubectl_delete_cmd(app_yaml_paths[i])

        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise
        except:
            raise

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

    parser = argparse.ArgumentParser(description="Deploys a kops cluster that "
                                     "migrates data to nuvo volumes")
    parser.add_argument(
        '--kops_cluster_name', help='name of kops cluster for Nuvo Data Plane[default: ' +
        default_cluster_name + ']',
        default=default_cluster_name)
    parser.add_argument(
        '--nodes', help='number of nodes in the cluster [default=3]', type=int, default=3,
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
        '--time_limit', help='Elapsed time limit [default=3600]', type=int, default=3600)
    parser.add_argument(
        '--pod_poll_freq', help='Pod Poll Frequency [default=10]', type=int, default=10)

    # Copy Specific Arguments

    parser.add_argument(
        '--copy_name', help='Name of copy container [default=copy]', default='copy')
    parser.add_argument(
        '--copy_image', help='Path to Docker image for copy container '
        '[default=mfederw/nuvotar:latest]', default='mfederw/nuvotar:latest')
    parser.add_argument(
        '--volume_tarfiles', help='Comma separated list of volume tar file object names', default="data.tar,data.tar,data.tar")
    parser.add_argument(
        '--bucket_name', help='Bucket to get the tar files from',
        required=True)
    parser.add_argument(
        '--volume_prefixes', help='Comma separated list of volume name prefixes', default="twenty,thirty,forty")
    parser.add_argument(
        '--should_wait', help='Force copy container to wait for user interaction'
        '[default=false]', default='false')
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
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

    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"
    if not args.config_dirpath:
        home_dir = os.environ.get('HOME') if os.environ.get('HOME') else "~"
        args.config_dirpath = home_dir + "/testing/config/"
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    args.list_volume_sizes = args.volume_sizes.strip().split(",")
    args.list_volume_prefixes = args.volume_prefixes.strip().split(",")
    args.list_volume_tarfiles = args.volume_tarfiles.strip().split(",")

    assert(args.list_volume_sizes), "'volume_sizes' param was empty"
    assert(args.list_volume_prefixes), "'volume_prefixes' param was empty"
    assert(args.list_volume_tarfiles), "'volume_tarfiles' param was empty"

    logging.info("len args.list_volume_sizes: %d", len(args.list_volume_sizes))
    logging.info("args.list_volume_sizes: %s", args.list_volume_sizes)
    logging.info("args.list_volume_prefixes: %s", args.list_volume_prefixes)
    logging.info("args.list_volume_tarfiles: %s", args.list_volume_tarfiles)

    print("Script to Copy data to NUVO volumes")
    test = CopyTool(args)
    test.run_copy_multiple_vols()

if __name__ == '__main__':
    main()
