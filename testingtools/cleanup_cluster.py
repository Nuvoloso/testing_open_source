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

from nuvoloso.dependencies.kops_cluster import KopsCluster
from nuvoloso.dependencies.kubectl_helper import KubectlHelper
from nuvoloso.api.nuvo_management import NuvoManagement

DEFAULT_ACCOUNT = "Demo Tenant"
SERVICES_POD_NAME = "services-0"
CENTRALD_CONTAINER = "centrald"
MGMT_NAMESPACE = "nuvoloso-management"
CLUSTER_DELETE_SCRIPT = "/opt/nuvoloso/bin/cluster_delete.py"


class deleteCluster:

    def __init__(self, args):
        self.kubectl_helper = KubectlHelper(args, context=args.mgmt_cluster_name)
        self.nuvo_mgmt = NuvoManagement(args)
        self.kops_obj = KopsCluster()
        self.cluster_name = args.nuvo_cluster_name
        self.delete_volumes = args.delete_volumes
        self.fail_requests = args.fail_requests
        self.account_name = args.account_name
        self.app_cluster_name = args.app_cluster_name
        self.kops_state_store = args.kops_state_store

    def delete_cluster(self):

        if self.app_cluster_name in self.kubectl_helper.get_all_contexts():
            logging.info("Running kops delete on %s", self.app_cluster_name)
            self.kops_obj.kops_delete_cluster(kops_cluster_name=self.app_cluster_name, kops_state_store=self.kops_state_store)
            logging.info("Kops delete ran successfully")
        
        cmd = [CLUSTER_DELETE_SCRIPT, "-y"]
        if self.cluster_name:
            cmd.append("-C %s" % self.cluster_name)
            domain_id = self.nuvo_mgmt.get_csp_domain_id_from_cluster(self.cluster_name)
            cmd.append("-D %s" % self.nuvo_mgmt.get_csp_domain(domain_id)[0]['name'])
        if self.delete_volumes:
            cmd.append("--delete-volumes")
        if self.fail_requests:
            cmd.append("--fail-requests")
        if self.account_name:
            cmd.append("-A '%s'" % self.account_name)

        logging.info("Calling cluster cleanup")
        self.kubectl_helper.run_kubectl_exec_cmd(' '.join(cmd), SERVICES_POD_NAME, CENTRALD_CONTAINER, namespace=MGMT_NAMESPACE)
        return

def main():

    parser = argparse.ArgumentParser(description="Deletes the specified kops cluster and cleans up the corresponding \
        cluster and volume objects in the kontroller")
    parser.add_argument(
        "--app_cluster_name", help="name of kops cluster for the application.", required=True)
    parser.add_argument(
        "--nuvo_cluster_name", help="Nuvo cluster name in kontroller which needs to be deleted.", required=True)
    parser.add_argument(
        "--mgmt_cluster_name", help="Kontroller cluster name.", required=True)
    parser.add_argument(
        "--kops_state_store", help="Kops state store used for the cluster", required=True)
    parser.add_argument(
        "--nuvo_kontroller_hostname", help="System hostname of the kontroller", required=True)
    parser.add_argument(
        "--log_dirpath", help="log dir to hold test and nuvo logs. Default: Current Directory", default=None)
    parser.add_argument(
        "--account_name", help="Account owner of the cluster. Default: Demo Tenant", default=DEFAULT_ACCOUNT)
    parser.add_argument(
        "--delete_volumes", help="Specify flag to permanently delete volumes, including their snapshots, consistency groups and \
            application groups", action="store_true", default=False)
    parser.add_argument(
        "--fail_requests", help="Specify flag to try and mark all outstanding VSRs and SRs as failed", action="store_true", default=False)
    
    args = parser.parse_args()
    home_dir = str(pathlib.Path.home())
    args.log_dirpath = args.log_dirpath if args.log_dirpath else home_dir + "/logs-" + \
            str(uuid.uuid4())[:5] + "/"
    pathlib.Path(args.log_dirpath).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(format='%(asctime)s %(message)s', filename=args.log_dirpath + "/" +
                        os.path.basename(__file__) + ".log", level=logging.INFO)
    args.kops_cluster_name = args.app_cluster_name
    cleanup = deleteCluster(args)
    cleanup.delete_cluster()
    return
    

if __name__ == "__main__":
    main()
