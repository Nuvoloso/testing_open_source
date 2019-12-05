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

Class to support KOPS commands
"""


import datetime
import logging
import os
import time
import subprocess
import tempfile
from ruamel.yaml import YAML
from botocore.exceptions import ClientError
import boto3

CHECK_OUTPUT_TIMEOUT = 360
# default k8s version
DEFAULT_K8S_VERSION = '1.14.8'

# default AMI to use for all instances
DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'

# default number of nodes in a kops cluster
DEFAULT_NODES = 1

# default node size
DEFAULT_NODE_SIZE = 't2.large'

# default volume size for slave nodes
DEFAULT_NODE_VOLUME_SIZE = 10

# default volume size for master node
DEFAULT_MASTER_VOLUME_SIZE = 20

DEFAULT_REGION = 'us-west-2'

DEFAULT_ZONE = 'us-west-2c'

KOPS_DELETE_CLUSTER = "kops delete cluster --name %s --state %s --yes"
WAIT_TIMEOUT = 600

IDLE_TIMEOUT = 3600

logger = logging.getLogger(__name__)


class KopsCluster(object):
    '''Class to support KOPS commands'''

    @staticmethod
    def get_home():
        '''Returns home dir of host'''
        return os.environ.get('HOME') if os.environ.get('HOME') else "~"

    @staticmethod
    def kops_cluster_validate(args):
        '''Validate KOPS cluster'''

        kops_cluster_validate = [
            'kops',
            'validate',
            'cluster',
            '--name=%s' % args.kops_cluster_name,
            '--state=%s' % args.kops_state_store
        ]
        success_str = "Your cluster " + args.kops_cluster_name + " is ready"
        response = False
        try:
            logger.info("Running kops cmd: %s", kops_cluster_validate)
            result = subprocess.check_output(" ".join(kops_cluster_validate),
                                             stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result:
                logger.info(result)
                response = True if success_str in result else False
            return response
        except subprocess.CalledProcessError as err:
            if err.output:
                logger.info(err.output)
            return False

    @staticmethod
    def kops_cluster_update(args, yes_option):
        '''KOPS cluster update'''

        kops_cluster_update = [
            'kops',
            'update',
            'cluster',
            '--name=%s' % args.kops_cluster_name,
            '--state=%s' % args.kops_state_store
        ]
        if yes_option:
            kops_cluster_update.append('--yes')

        kops_rolling_update = [
            'kops',
            'rolling-update',
            'cluster',
            '--name=%s' % args.kops_cluster_name,
            '--state=%s' % args.kops_state_store
        ]
        if yes_option:
            kops_rolling_update.append('--yes')

        try:
            logger.info("Running kops cmd: %s", kops_cluster_update)
            result = subprocess.check_output(" ".join(kops_cluster_update),
                                             stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if not result:
                raise Exception("kops update cluster yielded no output")
            logger.info(result)
            if "already exists" in result:
                raise Exception("Nuvo cluster ALREADY exists. Delete that first?")
            elif "kops rolling-update cluster" in result:
                logger.info("Running kops cmd: %s", kops_rolling_update)
                result = subprocess.check_output(" ".join(kops_rolling_update),
                                                 stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                logger.info(result)
                logger.info("count: %s", str(result.count("Ready")))
                if result.count("Ready") == args.nodes + 1:
                    logger.info("All nodes in kops cluster are ready")
                else:
                    # TODO
                    pass
            elif "Cluster is starting.  It should be ready in a few minutes" in result:
                time_now = datetime.datetime.utcnow()
                cmd_succeeded = False
                while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                        seconds=WAIT_TIMEOUT)):
                    time.sleep(60)
                    success = KopsCluster.kops_cluster_validate(args)
                    if success:
                        cmd_succeeded = True
                        break
                    else:
                        logger.info("Cluster is not ready yet. Sleeping for 60 seconds.")
                if not cmd_succeeded:
                    raise Exception("TIMEOUT (%s seconds) waiting for kops cluster %s to go "
                                    "to 'ready' state." % (WAIT_TIMEOUT, args.kops_cluster_name))
        except subprocess.CalledProcessError as err:
            if err.output:
                print(err.output)
                logger.info(err.output)
            logger.info("kops update cluster failed. Running - validate cluster now")
            output = KopsCluster.kops_cluster_validate(args)
            if output:
                print(output)
                logger.info(output)
            raise


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

    @staticmethod
    def create_kops_app_cluster(args):
        '''Create KOPS cluster'''

        kubernetes_version = args.kubernetes_version if args.kubernetes_version else \
            DEFAULT_K8S_VERSION
        image = args.image if args.image else DEFAULT_AMI
        k8s_master_zone = args.k8s_master_zone if args.k8s_master_zone else DEFAULT_ZONE
        k8s_nodes_zone = args.k8s_nodes_zone if args.k8s_nodes_zone else DEFAULT_ZONE
        nodes = args.nodes if args.nodes else DEFAULT_NODES
        node_size = args.node_size if args.node_size else DEFAULT_NODE_SIZE
        master_size = args.master_size if args.master_size else DEFAULT_NODE_SIZE
        node_volume_size = args.node_volume_size if args.node_volume_size else \
            DEFAULT_NODE_VOLUME_SIZE
        master_volume_size = args.master_volume_size if args.master_volume_size else \
            DEFAULT_MASTER_VOLUME_SIZE
        
        KopsCluster.check_for_ubuntu_bionic(args)

        ssh_path = KopsCluster.get_home() + "/.ssh/id_rsa.pub "
        cmd_kops_cluster_create = [
            'kops',
            'create',
            'cluster',
            '--node-count=%d' % nodes,
            '--node-volume-size=%s' % node_volume_size,
            '--node-size=%s' % node_size,
            '--master-size=%s' % master_size,
            '--master-volume-size=%s' % master_volume_size,
            '--master-zones=%s' % k8s_master_zone,
            '--zones=%s' % k8s_nodes_zone,
            '--image=%s' % image,
            '--kubernetes-version=%s' % kubernetes_version,
            '--state=%s' % args.kops_state_store,
            '--ssh-public-key=%s' % ssh_path,
            '--name=%s' % args.kops_cluster_name
        ]

        logger.info("Running kops cmd: %s", cmd_kops_cluster_create)
        try:
            logger.info("Creating kops cluster %s", args.kops_cluster_name)
            result = subprocess.check_output(" ".join(cmd_kops_cluster_create),
                                             stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result:
                logger.info(result)
                if "Finally configure your cluster with" not in result:
                    raise Exception("Did not find 'Finally configure your cluster with' "
                                    "in output of 'kops create cluster' cmd")
                KopsCluster.kops_cluster_edit_csi(args)
                KopsCluster.kops_edit_idle_timeout(args)
                KopsCluster.kops_cluster_update(args, True)
            else:
                raise Exception("No output from 'kops create cluster' cmd. Something went wrong??")
        except subprocess.CalledProcessError as err:
            if not err.output or "already exists" not in err.output:
                if err.output:
                    print(err.output)
                    logger.info(err.output)
                logger.info("kops create cluster failed. Running - validate cluster now")
                output = KopsCluster.kops_cluster_validate(args)
                if output:
                    print(output)
                    logger.info(output)
                raise
            logger.info(err.output)
            logger.info("DEBUG: kops cluster exists. Updating it now..")
            KopsCluster.kops_cluster_update(args, True)

    @staticmethod
    def kops_delete_cluster(kops_cluster_name, kops_state_store):
        """kops delete cluster"""

        kops_cluster_name = kops_cluster_name if kops_cluster_name else self.args.kops_cluster_name
        kops_state_store = kops_state_store if kops_state_store else self.args.kops_state_store
        kops_del_cmd =  (KOPS_DELETE_CLUSTER) % (kops_cluster_name, kops_state_store)
        logging.info("Runnging cmd: %s", kops_del_cmd)
        try:
            # Delete kops cluster (containing app + clusterd)
            result = subprocess.check_output(kops_del_cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info(result)
        except subprocess.CalledProcessError as err:
            if err.output:
                print(err.output)
                logging.info(err.output)
            raise

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

        data = yaml.dump(cluster)

        print(data)

        tmpname = ''
        with tempfile.NamedTemporaryFile(mode='w', prefix='cluster', suffix='.yaml', delete=False) as myfile:
            tmpname = myfile.name
            myfile.write(data)

        cmd_list = ['kops', 'replace', '-f', tmpname, '--state=%s' % state_store]
        print(cmd_list)

        retcode = subprocess.call(cmd_list)

        if retcode == 0:
            os.remove(tmpname)
        else:
            print('preserved temp file', tmpname)
        return retcode

    @staticmethod
    def kops_edit_idle_timeout(args, timeout=IDLE_TIMEOUT):
        """ 
        Set AWS Load balancer idle timeout. By default, kops sets it to 300s. For long running kubectl exec commands, we will need to increase it.
        If no timeout value is passed, this function will set it to 60 mins, which the max time allowed by AWS
        """

        cluster_name = args.kops_cluster_name
        state_store = args.kops_state_store

        cmd_list = [
            'kops', 'get', 'cluster', '--name=%s' % cluster_name, '--state=%s' % state_store,
            '-o', 'yaml'
        ]

        logger.info("Executing %s", cmd_list)

        try:
            data = subprocess.check_output(cmd_list)
        except subprocess.CalledProcessError as exc:
            logger.error("Failed to get cluster configuration for %s", cluster_name)
            if data:
                logger.error(data)
            raise

        yaml = YAML()
        try:
            cluster = yaml.load(data)
            cluster['spec']['api']['loadBalancer']['idleTimeoutSeconds'] = timeout
        except KeyError as exc:
            logger.error("Yaml data is probably not valid.")
            logger.error(cluster)
            raise

        with tempfile.NamedTemporaryFile(mode='w', prefix='cluster', suffix='.yaml', delete=False) as myfile:
            tmpname = myfile.name
            myfile.write(yaml.dump(cluster))

        assert tmpname
        cmd_list = ['kops', 'replace', '-f', tmpname, '--state=%s' % state_store]
        logger.info("Executing %s", cmd_list)

        try:
            ret = subprocess.call(cmd_list)
        except:
            logger.error("Failed to execute %s", cmd_list)
            logger.error(ret)
            logger.error("Deployment file saved to %s", tmpname)
            raise

        os.remove(tmpname)
