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
"""
import os
import pathlib
import argparse
import uuid
import logging 
import subprocess

from nuvoloso.dependencies.install_packages import InstallPackages
from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement

DEFAULT_AMI = '099720109477/ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-20190212.1'
DEFAULT_CENTRALD_CLUSTER_NAME = "nuvotestfiomulti.k8s.local"
NUVO_CLUSTER_NAME = "./nuvo_cluster_name.txt"

class CreateAppCluster:
    def __init__(self, args):
        self.args = args
        if not args.create_only:
            self.nuvo_mgmt = NuvoManagement(args)
        self.kubectl_helper = KubectlHelper(args)

    def install_dependencies(self):
        InstallPackages.apt_get_update()
        InstallPackages.install_kops()
        InstallPackages.install_kubectl()
        InstallPackages.install_awscli()
        InstallPackages.configure_aws(self.args)
        InstallPackages.generate_sshkeypair()
        return

    def set_protection_domains(self, csp_domain_id, nuvo_cluster_name):
        accounts = self.nuvo_mgmt.get_all_accounts()
        admin_account = self.args.account_name
        for account in accounts:
            # Creating spa to authorize accounts, so it can set protection domains
            self.nuvo_mgmt.do_service_plan_allocation(nuvo_cluster_name, account['name'])
            self.nuvo_mgmt.switch_accounts(account['name'])
            protection_domain_id = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_protection_domain(protection_domain_id, csp_domain_id)
            self.nuvo_mgmt.switch_accounts(admin_account)

    def create_application_cluster(self):
        self.install_dependencies()
        KopsCluster.create_kops_app_cluster(self.args)
        if self.args.create_only:
            return

        try:
            csp_domain_id = self.nuvo_mgmt.create_csp_domain()
            nuvo_cluster_name = self.nuvo_mgmt.deploy_clusterd(csp_domain_id)
            logging.info("Nuvo cluster created : %s", nuvo_cluster_name)
            self.set_protection_domains(csp_domain_id, nuvo_cluster_name)
            snapshot_catalog_pd = self.nuvo_mgmt.create_protection_domain()
            self.nuvo_mgmt.set_snapshot_catalog_policy(snapshot_catalog_pd, csp_domain_id)
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise
        return nuvo_cluster_name

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
        '--nuvo_kontroller_hostname', help='Hostname of https svc of Nuvo Kontroller')
    parser.add_argument(
        '--log_dirpath', help='log dir to hold test and nuvo logs', default=None)
    parser.add_argument(
        '--kubernetes_version', help='version of kubernetes to deploy', default='1.14.8')
    parser.add_argument(
        '--image', help='AMI Image for all instances', default=DEFAULT_AMI)
    parser.add_argument(
        '--node_volume_size', help='volume size for slave nodes of k8s cluster', type=int,
        default=10)
    parser.add_argument(
        '--master_volume_size', help='volume size for master node of k8s cluster', type=int,
        default=20)
    parser.add_argument(
        '--account_name', help='Nuvoloso account name', default='Demo Tenant')
    parser.add_argument(
        '--create_only', help='Create cluster only, skip Nuvoloso config', action='store_true')

    args = parser.parse_args()
    assert(args.kops_cluster_name and args.region and args.kops_state_store and args.aws_access_key
           and args.aws_secret_access_key), "Some/all input parameters are not filled. Aborting"

    if not args.create_only and args.nuvo_kontroller_hostname == None:
        logging.error("Must specify nuvo kontroller hostname")
        return

    home_dir = pathlib.Path.home()
    args.log_dirpath = args.log_dirpath if args.log_dirpath else str(home_dir.joinpath("logs-%s" % str(uuid.uuid4())[:5]))
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=pathlib.Path(args.log_dirpath).joinpath(
        "%s.log" % os.path.basename(__file__)), level=logging.INFO)

    print("Script to deploy an application cluster")
    test = CreateAppCluster(args)
    nuvo_cluster_name = test.create_application_cluster()

    if not args.create_only:
        with open(NUVO_CLUSTER_NAME, 'w') as fd:
            fd.write(nuvo_cluster_name)

        logging.info("Application cluster name created: %s", nuvo_cluster_name)
    else:
        logging.info("Application cluster created: %s", args.kops_cluster_name)


if __name__ == '__main__':
    main()

