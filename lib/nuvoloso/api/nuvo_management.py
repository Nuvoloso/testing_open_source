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

Supports calls to Nuvoloso Management Plane
"""


import datetime
import json
import logging
import os
import random
import time
import subprocess
import uuid
import requests
import yaml
import sys
import pathlib


ACCOUNTS = "accounts"
API_VERSION = "api/v1/"
CLUSTERS = "clusters"
CSP_CREDENTIALS = "csp-credentials"
CSP_DOMAINS = "csp-domains"
CSP_DOMAIN_TYPE = "aws"
DEFAULT_AWS_REGION = "us-west-2"
DEFAULT_AWS_ZONE = "us-west-2c"
DEFAULT_NAMESPACE = 'default'
DEFAULT_PVC_NAME = "my-pvc"
DEFAULT_SERVICE_PLAN = "General"
NAMESPACE_MANAGED_CLUSTER = "nuvoloso-cluster"
NODES = 'nodes'
NUVOLOSO_SECRET_NAME = "nuvoloso-account"
PERSISTENT_VOLUME_SPEC = "persistent-volume-spec"
PROTECTION_DOMAIN = "protection-domains"
SERVICE_PLANS = "service-plans"
SERVICE_PLAN_ALLOCATIONS = "service-plan-allocations"
SNAPSHOTS = "snapshots"
STORAGE = "storage"
STORAGE_REQUESTS = "storage-requests"
SYSADMIN = "admin"
SYSADMIN_PWD = "admin"
VOLUME_SERIES = "volume-series"
VOL_SERIES_REQUESTS = 'volume-series-requests'
WAIT_TIMEOUT = 18000

logger = logging.getLogger(__name__)

sys.path.append(str(pathlib.Path(__file__).parent.parent.absolute()))
from dependencies.kubectl_helper import KubectlHelper
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class NuvoManagement(object):
    '''Supports calls to Nuvoloso Management Plane'''

    TOTAL_SPA_CAPACITY = 5000

    def __init__(self, args):

        self.args = args
        self.session = requests.Session()
        self.session.verify = False
        self.headers = {'content-type': 'application/json'}
        self.url_base = "https://" + self.args.nuvo_kontroller_hostname + "/"
        self.authorize()
        self.url_base = self.url_base + API_VERSION
        logger.info("self.url_base: %s", self.url_base)
        self.url_vsr = self.url_base + VOL_SERIES_REQUESTS
        # Find account id for 'headers'
        self.account_id = self.find_account_id()
        self.kubectl_helper = KubectlHelper(args, args.kops_cluster_name)
        self.update_headers()

    def _api_request(self, http_verb, url, json_data=None, query_params=None):
        logger.info("API request [%s] URL: %s data: %s, Query Params: %s headers: %s", http_verb, url, json_data, query_params, self.headers)
        if http_verb == "POST":
            response = self.session.post(url, headers=self.headers, data=json_data, params=query_params)
            status_code = [200, 201]
        elif http_verb == "GET":
            response = self.session.get(url, headers=self.headers, data=json_data, params=query_params)
            status_code = [200]
        elif http_verb == "PATCH":
            response = self.session.patch(url, headers=self.headers, data=json_data, params=query_params)
            status_code = [200]
        else:
            raise ValueError("Unexpected verb for http request")
        logger.info("Response: %s", response.json())

        if response.status_code not in status_code:
            if (response.status_code == 403 and response.json()['message'] == "Expired token") \
                or response.status_code == 401:
                logger.info("%s. Re-authorizing and retrying http request", response.json()['message'])
                self.authorize()
                return self._api_request(http_verb, url, json_data, query_params)
            else:
                raise RuntimeError("%s request to %s failed with status code: %s and message: %s" % (http_verb, url, response.status_code, response.json()), response.status_code, response.json()['message'])

        return response.json()

    def api_request(self, *args, **kwargs):
        """Make an API request with the authority of the configured account"""
        return self._api_request(*args, **kwargs)

    def authorize(self):
        # Find token Id for 'headers'
        url = "https://" + self.args.nuvo_kontroller_hostname + "/" + "auth/login"
        data = {"username": SYSADMIN, "password": SYSADMIN_PWD}
        response = self._api_request("POST", url, json_data=json.dumps(data))
        if not response['token']:
            raise Exception("User: 'admin' failed to login to '/auth/login'")
        self.token = response['token']
        self.headers['x-auth'] = self.token

    def update_snapshot_policy_account(self):
        '''Update account to turn on default snapshotCreation Policy'''

        logger.info("Before Updating the account:")
        url = self.url_base + ACCOUNTS + "/" + self.account_id
        response = self._api_request("GET", url)
        orig_snapshot_mgmt_policy = response["snapshotManagementPolicy"]
        logger.info("orig_snapshot_mgmt_policy: %s", orig_snapshot_mgmt_policy)
        logger.info("Updating accountId: %s to turn off automatic snapshots", self.account_id)
        url = url + "?set=snapshotManagementPolicy"
        data = {}
        data["snapshotManagementPolicy"] = orig_snapshot_mgmt_policy
        data["snapshotManagementPolicy"]["disableSnapshotCreation"] = True
        data["snapshotManagementPolicy"]["inherited"] = False
        logger.info("url: %s", url)
        logger.info("data: %s", data)
        response = self._api_request("PATCH", url, json_data=json.dumps(data))
        logger.info("Updated account:")
        url = self.url_base + ACCOUNTS + "/" + self.account_id

    def update_headers(self, account_id=None):
        ''' Update token and account id in headers '''
        self.headers['x-auth'] = self.token
        self.headers['x-account'] = self.account_id if not account_id else account_id

        return

    def find_account_id(self, account_name=None):
        '''Return account id'''

        url = self.url_base + "users?authIdentifier=" + SYSADMIN
        self.headers['x-auth'] = self.token
        response = self._api_request("GET", url)
        if not account_name:
            account_name = self.args.account_name

        try:
            roles = response[0]['accountRoles']
            for role in roles:
                if account_name == role['accountName']:
                    account_id = role['accountId']
                    break
        except IndexError:
            logger.info("Account name: %s does not exist", self.args.account_name)
            raise
        except:
            logger.error("Cannot find account id")
            raise
        return account_id

    def create_csp_credentials(self):
        '''Create a csp credential object. Returns the csp credential id'''

        url = self.url_base + CSP_CREDENTIALS
        csp_credential_name = "aws-keys-" + str(uuid.uuid4())[:5]
        data = {
                "name": csp_credential_name,
                "cspDomainType": CSP_DOMAIN_TYPE.upper(),
                "credentialAttributes": {
                    "aws_access_key_id": {
                        "kind": "STRING",
                        "value": self.args.aws_access_key,
                    },
                    "aws_secret_access_key": {
                        "kind": "SECRET",
                       "value": self.args.aws_secret_access_key
                    }
                },
            }
        logger.info("Creating a CSP credential object %s", csp_credential_name)
        response = self._api_request("POST", url, json_data=json.dumps(data))
        return response['meta']['id']

    def create_csp_domain(self):
        '''Create a new CSP domain'''

        # Get account id first
        url = self.url_base + CSP_DOMAINS
        csp_name = CSP_DOMAIN_TYPE + "-" + str(uuid.uuid4())[:5]
        csp_credential_id = self.create_csp_credentials()
        logger.info("Creating a new CSP domain: %s", csp_name)
        aws_master_zone = self.args.k8s_master_zone if self.args.k8s_master_zone else \
            DEFAULT_AWS_ZONE
        aws_region = self.args.region if self.args.region else \
            DEFAULT_AWS_REGION
        data = {"name":csp_name, "cspDomainType": CSP_DOMAIN_TYPE.upper(),
                "cspCredentialId": csp_credential_id,
                "cspDomainAttributes": {
                    "aws_region":{"kind": "STRING",
                                  "value": aws_region},
                    "aws_availability_zone": {"kind": "STRING",
                                              "value": aws_master_zone}},
                "managementHost":self.args.nuvo_kontroller_hostname, "accountId": self.account_id}
        response = self._api_request("POST", url, json_data=json.dumps(data))
        return response['meta']['id']

    def get_csp_id(self, csp_name):
        '''Get UUID of this newly created CSP domain'''

        url = self.url_base + CSP_DOMAINS + "?name=" + csp_name
        response = self._api_request("GET", url)
        # API returns a list of one object so index of 0 is needed
        return response[0]['meta']['id']

    def get_csp_domain_id_from_cluster(self, cluster_name):
        ''' Returns the csp domain id of a cluster '''

        return self.get_cluster_details(cluster_name)['cspDomainId']

    def get_csp_domain(self, csp_domain_id=None):
        '''Return list of CSP domain(s)'''
        url = self.url_base + CSP_DOMAINS
        response = self._api_request("GET", url)
        if csp_domain_id:
            response = [x for x in response if x['meta']['id'] == csp_domain_id]
        return response

    def deploy_clusterd(self, csp_id, nuvo_cluster_name=None):
        '''deploy cluster for nuvo'''

        nuvodeployment_filename = self.args.log_dirpath + "/nuvo-dataplane-" + str(uuid.uuid4())[:5] + \
            ".yaml"
        logger.info("Generating and downloading cluster yaml file: %s",
                    nuvodeployment_filename)
        if not nuvo_cluster_name:
            nuvo_cluster_name = "nuvoauto-" + str(uuid.uuid4())[:5]

        # Create cluster object and get orchestrator YAML

        data = {
            "clusterType": 'kubernetes',
            "name": nuvo_cluster_name,
            "cspDomainId": csp_id
        }
        url = self.url_base + CLUSTERS
        response = self._api_request("POST", url, json_data=json.dumps(data))

        cluster_id = response['meta']['id']

        url = self.url_base + CLUSTERS + "/" + cluster_id + "/orchestrator"
        response = self._api_request("GET", url)
        f_handle = open(nuvodeployment_filename, "w")
        f_handle.write(response['deployment'])
        f_handle.close()

        logger.info("Deploy cluster yaml file to create nuvo cluster: %s",
                    nuvo_cluster_name)
        try:
            if not os.path.isfile(nuvodeployment_filename):
                raise Exception("File: " + nuvodeployment_filename +
                                " NOT found. Aborting..")
            result = self.kubectl_helper.deploy(nuvodeployment_filename)
            logger.info(result)
            time_now = datetime.datetime.utcnow()
            cmd_succeeded = False
            while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(
                    seconds=WAIT_TIMEOUT)):
                time.sleep(60)
                result = self.kubectl_helper.get_pods(namespace="nuvoloso-cluster")
                if result:
                    logger.info(result)
                    if result.count("Running") == self.args.nodes + 1:
                        logger.info("All pods in nuvo cluster are running fine")
                        cmd_succeeded = True
                        time.sleep(60)
                        break
                    else:
                        logger.info("Nuvo cluster is not ready yet. "
                                    "Sleeping for 60 seconds..")
                else:
                    raise Exception("Failed while deploying Nuvo cluster. Get pods did not yield any response")
            if not cmd_succeeded:
                raise Exception("TIMEOUT (%s seconds) waiting for nuvoloso-management cluster to"
                                "  go to 'Running' state." % WAIT_TIMEOUT)
            return nuvo_cluster_name
        except subprocess.CalledProcessError as err:
            if err.output:
                logger.info(err.output)
            output = self.kubectl_helper.describe_pods(namespace="nuvoloso-cluster")
            if output:
                logger.info(output)
            raise

    def get_cluster_id(self, nuvo_cluster_name):
        '''Get UUID of cluster'''

        url = self.url_base + CLUSTERS + "?name=" + nuvo_cluster_name
        response = self._api_request("GET", url)
        logger.info("cluster id: %s", response[0]['meta']['id'])
        return response[0]['meta']['id']

    def get_cluster_details(self, nuvo_cluster_name, domain_id=None, ouput='json'):
        ''' Get details of a cluster '''

        url = self.url_base + CLUSTERS
        query_params = dict(name=nuvo_cluster_name)
        if domain_id:
            query_params['cspDomainId'] = domain_id
        response = self._api_request("GET", url, query_params=query_params)
        logger.info("cluster details: %s", response[0])
        return response[0]

    def wait_for_cluster_state(self, nuvo_cluster_name, state, domain_id=None):
        ''' Wait till cluster goes to the specified state '''

        time_now = datetime.datetime.utcnow()
        cmd_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            cur_state = self.get_cluster_details(nuvo_cluster_name, domain_id=domain_id)['state']
            if cur_state == state:
                cmd_succeeded = True
                logger.info("Cluster is now in %s", cur_state)
                break
            else:
                logger.info("Cluster state is still in %s. Expected to go to %s. Retrying after 30s", cur_state, state)
                time.sleep(30)

        if not cmd_succeeded:
            raise RuntimeError("Timeout %ss exceeded. Cluster state is still in %s" % (WAIT_TIMEOUT, cur_state))
        return 

    def get_serviceplan_id(self):
        '''Get serviceplan UUID for a service plan '''

        serviceplan_name = getattr(self.args, "serviceplan_name", DEFAULT_SERVICE_PLAN)
        url = self.url_base + SERVICE_PLANS
        response = self._api_request("GET", url, query_params=dict(name=serviceplan_name))
        logger.info("serviceplan_id: %s", response[0]['meta']['id'])
        return response[0]['meta']['id']

    def get_authorized_account_id(self):
        '''Returns account_id'''

        logger.info("authorized_account_id: %s", self.account_id)
        return self.account_id

    def do_service_plan_allocation(self, nuvo_cluster_name, account_name):
        '''Do Service Plan Allocation -- returns SPA id if successful

        Expects the following to be set in args
        - account_id
        - serviceplan_name
        '''

        logger.info("Doing a new Service Plan Allocation")

        total_spa_capacity_in_bytes = self.TOTAL_SPA_CAPACITY * 1024 * 1024 * 1024

        # Set 'completed time as 5 minutes from now'
        complete_by_time = (datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).isoformat(
            timespec='seconds') + 'Z'
        data = {"completeByTime": complete_by_time, "requestedOperations": ["ALLOCATE_CAPACITY"],
                "servicePlanAllocationCreateSpec": {
                    "accountId": self.account_id,
                    "authorizedAccountId":
                    self.find_account_id(account_name),
                    "clusterId": self.get_cluster_id(nuvo_cluster_name),
                    "servicePlanId": self.get_serviceplan_id(),
                    "tags": ["_SERVICE_PLAN_ALLOCATION_COST.11"],
                    "totalCapacityBytes": total_spa_capacity_in_bytes}}
        logger.info("data: %s", data)
        response = self._api_request("POST", self.url_vsr, json_data=json.dumps(data))
        vsr_id = response['meta']['id']
        logger.info("vsr_id: %s", vsr_id)

        # Wait for VSR to succeed/fail
        time_now = datetime.datetime.utcnow()
        url = self.url_vsr + "/" + vsr_id
        vsr_succeeded = False
        spa_id = ''
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            response = self._api_request("GET", url)
            if response['volumeSeriesRequestState'] == 'SUCCEEDED':
                logger.info("DEBUG - vsr_id: %s SUCCEEDED", vsr_id)
                spa_id = response['servicePlanAllocationId']
                vsr_succeeded = True
                break
            elif response['volumeSeriesRequestState'] == 'FAILED':
                logger.info("DEBUG - vsr_id: %s FAILED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise Exception("VSR for SPA Allocation FAILED")
            else:
                logger.info("VSR state is: %s. Sleeping for 30 seconds to wait for VSR to go to "
                            "SUCCEEDED/FAILED state", response['volumeSeriesRequestState'])
                time.sleep(30)

        if not vsr_succeeded:
            raise Exception("TIMEOUT (%s seconds) waiting for VSR %s to succeed." %
                            WAIT_TIMEOUT, vsr_id)
        return spa_id

    def create_volume(self, nuvo_cluster_name, vol_size=1, vol_name_prefix="nuvoauto-vol-",
                      serviceplan_name=None):
        """Create volume object"""

        logger.info("Creating a new Volume Object")

        serviceplan_name = self.args.serviceplan_name if self.args.serviceplan_name \
            else DEFAULT_SERVICE_PLAN
        complete_by_time = (datetime.datetime.utcnow() + datetime.timedelta(minutes=60)).isoformat(
            timespec='seconds') + 'Z'
        volume_name = vol_name_prefix + str(uuid.uuid4())[:5]
        # convert GB to Bytes
        vol_size_bytes = int(vol_size) << 30
        logger.info("vol_size_bytes: %s", str(vol_size_bytes))
        data = {"clusterId":self.get_cluster_id(nuvo_cluster_name),
                "completeByTime": complete_by_time,
                "requestedOperations": ["CREATE", "BIND", "PUBLISH"],
                "volumeSeriesCreateSpec":{
                    "accountId": self.account_id,
                    "name": volume_name,
                    "servicePlanId": self.get_serviceplan_id(),
                    "sizeBytes": vol_size_bytes, "tags":[]}}
        logger.info("self.url_vsr: %s", self.url_vsr)
        logger.info("payload for create_volume request: %s", data)

        response = self._api_request("POST", self.url_vsr, json_data=json.dumps(data))
        vsr_id = response['meta']['id']
        logger.info("vsr_id: %s", vsr_id)

        # Wait for VSR to succeed/fail
        time_now = datetime.datetime.utcnow()
        url = self.url_vsr + "/" + vsr_id
        vsr_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            response = self._api_request("GET", url)
            if response['volumeSeriesRequestState'] == 'SUCCEEDED':
                logger.info("DEBUG - vsr_id: %s SUCCEEDED", vsr_id)
                vsr_succeeded = True
                break
            elif response['volumeSeriesRequestState'] == 'FAILED':
                logger.info("DEBUG - vsr_id: %s FAILED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise Exception("VSR for SPA Allocation FAILED")
            elif response['volumeSeriesRequestState'] == 'CANCELED':
                logger.info("DEBUG - vsr_id: %s CANCELED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise RuntimeError("VSR CANCELED")
            else:
                logger.info("VSR state is: %s. Sleeping for 30 seconds to wait for VSR to go to "
                            "SUCCEEDED/FAILED state", response['volumeSeriesRequestState'])
                time.sleep(30)

        if not vsr_succeeded:
            logger.info("vsr state details: %s", response)
            raise Exception("TIMEOUT (%s seconds) waiting for VSR %s to succeed." %
                            WAIT_TIMEOUT, vsr_id)
        return volume_name

    def _get_volume_pvc(self, volume_name, vol_pvc_filepath=None, namespace=DEFAULT_NAMESPACE):
        """Get volume's pvc yaml file"""

        vol_id = self.get_volume_id(volume_name)
        url = self.url_base + VOLUME_SERIES + '/' + vol_id
        response = self._api_request("GET", url)

        if not response or not response['clusterDescriptor']['k8sPvcYaml']['value']:
            raise Exception("Error while trying to get PVC for volume: "
                            "%s vol_id: %s", volume_name, vol_id)
        new_pvc_name = DEFAULT_PVC_NAME + "-" + volume_name + "-" + str(uuid.uuid4())[:5]
        data_new = yaml.load(response['clusterDescriptor']['k8sPvcYaml']['value'])
        data_new['metadata']['name'] = new_pvc_name
        if namespace:
            data_new['metadata']['namespace'] = namespace

        if not vol_pvc_filepath:
            vol_pvc_filepath = self.args.log_dirpath + "/" + new_pvc_name + \
                ".yaml"

        with open(vol_pvc_filepath, "w") as f_handle:
            yaml.dump(data_new, f_handle, default_flow_style=False)
        return (new_pvc_name, vol_pvc_filepath)

    def get_volume_id(self, volume_name):
        '''Get vol_uuid'''

        url = self.url_base + VOLUME_SERIES + "?name=" + volume_name
        response = self._api_request("GET", url, query_params=dict(name=volume_name))
        logger.info("volume_id: %s", response[0]['meta']['id'])
        return response[0]['meta']['id']

    def get_volume_state(self, volume_name):
        ''' Get volume state '''

        url = self.url_base + VOLUME_SERIES + "?name=" + volume_name
        response = self._api_request("GET", url)
        logger.info("volume_state: %s", response[0]['volumeSeriesState'])
        return response[0]['volumeSeriesState']

    def get_consistencygroup_id(self, volume_name):
        '''Get consistencygroup_id of a volume'''

        url = self.url_base + VOLUME_SERIES + "?name=" + volume_name
        response = self._api_request("GET", url)
        logger.info("consistencygroup_id: %s", response[0]['consistencyGroupId'])
        return response[0]['consistencyGroupId']

    def launch_volume_pvc(self, volume_name, vol_pvc_filepath=None, pvc_name=None, namespace=DEFAULT_NAMESPACE):
        """Creates and launches PVC for statically provisioned volume"""

        if (not vol_pvc_filepath and pvc_name) or (vol_pvc_filepath and  not pvc_name):
            raise Exception("Either pass in pvc_name and vol_pvc_filepath or neither")
        if not vol_pvc_filepath:
            (pvc_name, vol_pvc_filepath) = self._get_volume_pvc(volume_name, namespace=namespace)
        vol_id = self.get_volume_id(volume_name)

        logger.info("Deploy PVC yaml file: %s to create a pvc for volume: %s in "
                    "kubernetes", vol_pvc_filepath, volume_name)
        try:
            result = self.kubectl_helper.deploy(vol_pvc_filepath)
            if result:
                logger.info(result)
            result = self.kubectl_helper.get_pvc(pvc_name=pvc_name, namespace=namespace)
            if result:
                logger.info(result)
            if pvc_name in result and vol_id in result and (result.count(
                    "Bound") == 1):
                logger.info("PVC: %s for Volume id: %s was created in Kubernetes", pvc_name, vol_id)
                return pvc_name
            else:
                raise Exception("Volume's get_pvc shows incorrect data")
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            raise

    def backup_consistencygroup(self, nuvo_cluster_name, volume_name, wait_for_vsr=True, max_retries=3):
        '''Run backup on Nuvo volume'''

        logger.info("Starting a new backup")
        complete_by_time = (datetime.datetime.utcnow() + datetime.timedelta(minutes=60)).isoformat(
            timespec='seconds') + 'Z'
        data = {"clusterId":self.get_cluster_id(nuvo_cluster_name),
                "consistencyGroupId":self.get_consistencygroup_id(volume_name),
                "completeByTime":complete_by_time,
                "requestedOperations":["CG_SNAPSHOT_CREATE"]}
        logger.info("self.url_vsr: %s", self.url_vsr)
        logger.info("payload for backup request: %s", data)

        for i in range(max_retries+1):
            try:
                response = self._api_request("POST", self.url_vsr, json_data=json.dumps(data))
                vsr_id = response['meta']['id']
                backup_vsr_id = vsr_id
                if not wait_for_vsr:
                    logger.info("wait_for_vsr is set to %s", wait_for_vsr)
                    return backup_vsr_id
                else:
                    # Wait for VSR to succeed/fail
                    self.wait_for_vsr(backup_vsr_id)
                    return backup_vsr_id
            except RuntimeError as e:
                if i < max_retries:
                    logger.info("Hit RuntimeError. Retrying again. Attempt: %s/%s", i+1, max_retries)
                    pass
                else:
                    logger.error("Hit max retries. Snapshot backup failed.")
                    raise

    def get_snapshot_id(self, volume_id, backup_vsr_id):
        '''Selecting snapshot_id to restore from. Take it from volume-series'''

        url = self.url_base + SNAPSHOTS
        response = self._api_request("GET", url, query_params=dict(volumeSeriesId=volume_id))
        if response:
            for curr_snap in response:
                # flatten all system tags (key0:value0 \t key1:value1) as tab separated string
                flat_tags_str = '\t'.join(curr_snap['systemTags'])
                # if backup_vsr_id is in systemTags of this snapshot, we've found the snapshot
                if backup_vsr_id in flat_tags_str:
                    return curr_snap['snapIdentifier']
        logger.info("Failed to find the snapshot identifier to restore from "
                    "response: %s", response)
        raise Exception("Couldn't find snapshot id for volume: %s  backup_vsr_id: %s" % volume_id, backup_vsr_id)

    def get_snapshot_object_id(self, volume_id, backup_vsr_id):
        '''Selecting snapshot_id to restore from. Take it from volume-series'''

        url = self.url_base + "snapshots"
        response = self._api_request("GET", url, query_params=dict(volumeSeriesId=volume_id))
        if response:
            for curr_snap in response:
                # flatten all system tags (key0:value0 \t key1:value1) as tab separated string
                flat_tags_str = '\t'.join(curr_snap['systemTags'])
                # if backup_vsr_id is in systemTags of this snapshot, we've found the snapshot
                if backup_vsr_id in flat_tags_str:
                    return curr_snap['meta']['id']
        logger.info("Failed to find the snapshot identifier to restore from "
                    "response: %s", response)
        raise Exception("Couldn't find snapshot id for volume: %s  backup_vsr_id: %s" % volume_id, backup_vsr_id)

    def get_node_id(self, cluster_id):
        '''Select a node randomly, for restore operation'''

        node_id = None
        url = self.url_base + NODES
        logger.info("Getting nodes to select from (for restore)...")
        response = self._api_request("GET", url, query_params=dict(clusterId=cluster_id))
        if response:
            node_id = random.choice(response)['meta']['id']
            logger.info("Selecting node id: %s for restore", node_id)
            return node_id
        else:
            if response:
                logger.info(str(response))
            raise Exception("FAILED to get nodes in the cluster_id: %s" % cluster_id)

    def wait_for_vsr(self, vsr_id):
        ''' Wait for a VSR to complete '''

        time_now = datetime.datetime.utcnow()
        vsr_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            response = self._api_request("GET", self.url_vsr + "/" + vsr_id)
            logger.debug("DEBUG - Restore VSR (has restored volume id + vsr state) data: %s", response)
            if response['volumeSeriesRequestState'] == 'SUCCEEDED':
                logger.debug("DEBUG - restore_vsr_id: %s SUCCEEDED", vsr_id)
                vsr_succeeded = True
                break
            elif response['volumeSeriesRequestState'] == 'FAILED':
                logger.info("DEBUG - restore_vsr_id: %s FAILED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise RuntimeError("VSR for restore FAILED")
            elif response['volumeSeriesRequestState'] == 'CANCELED':
                logger.info("DEBUG - vsr_id: %s CANCELED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise RuntimeError("VSR CANCELED")
            else:
                logger.info("VSR state is: %s. Sleeping for 30 seconds to wait for VSR to go to "
                            "SUCCEEDED/FAILED state", response['volumeSeriesRequestState'])
                time.sleep(30)

        if not vsr_succeeded:
            logger.info("vsr state details: %s", response)
            raise RuntimeError("TIMEOUT (%s seconds) waiting for restore VSR %s to succeed." %
                            WAIT_TIMEOUT, vsr_id)
        return response

    def restore_volume(self, volume_name, nuvo_cluster_name, backup_vsr_id, prefix_str="RESTORE_PREFIX", wait_for_vsr=True, max_retries=3):

        logger.info("Starting a new restore")
        complete_by_time = (datetime.datetime.utcnow() + datetime.timedelta(
            seconds=WAIT_TIMEOUT)).isoformat(timespec='seconds') + 'Z'
        restored_vol_name = prefix_str + volume_name
        data = {
            "snapshot": {
                    "volumeSeriesId": self.get_volume_id(volume_name)
            },
            "nodeId": self.get_node_id(self.get_cluster_id(nuvo_cluster_name)),
            "snapshotId": self.get_snapshot_object_id(self.get_volume_id(volume_name),
                                                      backup_vsr_id),
            "completeByTime": complete_by_time,
            "requestedOperations": ["CREATE_FROM_SNAPSHOT"],
            "volumeSeriesCreateSpec": {
                "name": restored_vol_name
            }
        }
        logger.info("self.url_vsr: %s", self.url_vsr)

        for i in range(max_retries+1):
            try:
                response = self._api_request("POST", self.url_vsr, json_data=json.dumps(data))
                restore_vsr_id = response['meta']['id']
                if not wait_for_vsr:
                    logger.info("wait_for_vsr is set to %s", wait_for_vsr)
                    return restore_vsr_id
                else:
                    response = self.wait_for_vsr(restore_vsr_id)
                    break
            except RuntimeError as e:
                if i < max_retries:
                    logger.info("Hit Runtime Exception. Retrying again. Retry attempt: %s/%s", i+1, max_retries)
                    pass
                else:
                    logger.error("Hit max retries. Snapshot restore failed")
                    raise

        restored_vol_id = None
        # Get the volume-series id (vol_id) of restored volume
        if response['systemTags'] and ":" in response['systemTags'][0]:
            # Look at all system tags and fetch the one with key: "vsr.newVolumeSeries"
            found_tag_list = list(filter(
                lambda system_tag: "vsr.newVolumeSeries" in system_tag, response['systemTags']))
            if not found_tag_list or len(found_tag_list) > 1:
                        raise ValueError("Failed to find key value pair for vsr.newVolumeSeries")
            system_tags = found_tag_list[0].split(":")
            if system_tags[0] == "vsr.newVolumeSeries":
                restored_vol_id = system_tags[1]
                if restored_vol_id:
                    logger.info("Restored volume series id: %s", restored_vol_id)
                else:
                    raise ValueError("Could not find id of restored volume in system_tags")
            else:
                raise ValueError("Could not find id of restored volume..")

        return restored_vol_name

    def backup_restore_volume(self, volume_name, nuvo_cluster_name, prefix_str="RESTORE_PREFIX"):
        '''Backup and Restore volume'''

        # back up the volume
        backup_vsr_id = self.backup_consistencygroup(nuvo_cluster_name, volume_name)
        # now run restore
        return self.restore_volume(volume_name, nuvo_cluster_name, backup_vsr_id, prefix_str=prefix_str)

    def _get_cluster_secret_yaml(self, cluster_name, cluster_secret_filepath=None, namespace=DEFAULT_NAMESPACE):
        """Get clusters's secret yaml file"""

        cluster_id = self.get_cluster_id(cluster_name)
        authorized_account_id = self.get_authorized_account_id()
        url = self.url_base + CLUSTERS + '/' + cluster_id + '/account-secret'
        response = self._api_request("GET", url, query_params=dict(authorizedAccountId=authorized_account_id))

        # Minimal validation
        if not response or not response['value']:
        # if not response:
            raise Exception("Error while trying to get secret for cluster: "
                            "%s authorizedAccountId: %s", cluster_name, authorized_account_id)

        if not cluster_secret_filepath:
            cluster_secret_filepath = self.args.log_dirpath + "/" + cluster_name + \
                "_cluster_secret.yaml"

        # Write out just the secret YAML data returned properly formatted
        data_new = yaml.load(response['value'])
        if namespace:
            data_new['metadata']['namespace'] = namespace
        with open(cluster_secret_filepath, "w") as f_handle:
            yaml.dump(data_new, f_handle, default_flow_style=False)
        return (cluster_secret_filepath)

    def launch_secret_yaml(self, cluster_name, cluster_secret_filepath=None, namespace=DEFAULT_NAMESPACE):
        """Gets and creates secret for cluster"""
        max_retries = 5
        if (not cluster_name):
            raise Exception("Must pass in cluster_name")
        if not cluster_secret_filepath:
            (cluster_secret_filepath) = self._get_cluster_secret_yaml(cluster_name, namespace=namespace)

        logger.info("Deploy secret yaml file: %s to create a secret for cluster: %s in "
                    "kubernetes", cluster_secret_filepath, cluster_name)
        retries = 0
        for i in range(max_retries+1):
            try:
                result = self.kubectl_helper.deploy(cluster_secret_filepath)
                if result:
                    logger.info(result)
                    break
            except subprocess.CalledProcessError as err:
                if err.output and i < max_retries:
                    logger.info(err.output)
                    raise
                else:
                    time.sleep(3)
                    try:
                        result = json.loads(self.kubectl_helper.get_secret(name="nuvoloso-account", namespace=namespace, format='json'))
                        if result['metadata']['name'] == "nuvoloso-account":
                            logger.info("Secret already created. Skipping creation")
                            break
                    except:
                        pass
        return

    def _get_volume_pvc_dynamic(self, pvc_name, spa_id, vol_size, vol_pvc_filepath=None, namespace=DEFAULT_NAMESPACE):
        """Get dynamic volume's pvc yaml file"""

        url = self.url_base + SERVICE_PLAN_ALLOCATIONS + '/' + spa_id
        logger.info("url: %s", url)
        response = self._api_request("GET", url)

        if not response or not response['clusterDescriptor']['k8sPvcYaml']['value']:
            raise Exception("Error while trying to get PVC for volume: "
                            "%s spa_id: %s", pvc_name, spa_id)

        data_new = yaml.load(response['clusterDescriptor']['k8sPvcYaml']['value'])
        data_new['metadata']['name'] = pvc_name
        vol_size_g = str(vol_size) + 'Gi'
        data_new['spec']['resources']['requests']['storage'] = vol_size_g
        if namespace:
            data_new['metadata']['namespace'] = namespace
        if not vol_pvc_filepath:
            vol_pvc_filepath = self.args.log_dirpath + "/" + pvc_name + \
                ".yaml"

        with open(vol_pvc_filepath, "w") as f_handle:
            yaml.dump(data_new, f_handle, default_flow_style=False)
        return (pvc_name, vol_pvc_filepath)

    def launch_volume_pvc_dynamic(self, pvc_name, spa_id, vol_size, vol_pvc_filepath=None, namespace=DEFAULT_NAMESPACE):
        """Creates and launches PVC for dynamically provisioned volume"""

        if not vol_pvc_filepath:
            (pvc_name, vol_pvc_filepath) = self._get_volume_pvc_dynamic(pvc_name, spa_id, vol_size, namespace=namespace)

        logger.info("Deploy PVC yaml file: %s to create a pvc for volume: %s in "
                    "kubernetes", vol_pvc_filepath, pvc_name)

        try:
            result = self.kubectl_helper.deploy(vol_pvc_filepath)
            if result:
                logger.info(result)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            raise

        time_now = datetime.datetime.utcnow()
        cmd_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            time.sleep(60)
            result = self.kubectl_helper.get_pvc(pvc_name=pvc_name, output='yaml', namespace=namespace)
            if result:
                logger.info(result)
                data = yaml.load(result)
                if data['metadata']['name'] == pvc_name and data['status']['phase'] == "Bound":
                    logger.info("PVC: %s for SPA id: %s was created in Kubernetes", pvc_name, spa_id)
                    cmd_succeeded = True
                    print(data)
                    volume_name = data['spec']['volumeName']
                    time.sleep(5)
                    break
                else:
                    logger.info('PVC is not ready yet.  Sleeping for 60 seconds...')
            else:
                raise Exception('Get pvc failed')

        if not cmd_succeeded:
                raise Exception("TIMEOUT (%s seconds) waiting for pvc to complete" % WAIT_TIMEOUT)
        return volume_name

    def bind_and_publish_volume(self, volume_name, cluster_name):
        """ Binds volume to the specified cluster and publishes volume """

        time_now = datetime.datetime.utcnow()
        volume_state_good = False
        logger.info("Check if volume %s is in UNBOUND state", volume_name)
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            volume_status = self.get_volume_state(volume_name)
            if volume_status == "UNBOUND":
                logger.info("Volume state is now UNBOUND. Snapshots should be created and uploaded.")
                volume_state_good = True
                break
            else:
                time.sleep(60)
                logger.info("Volume state is now %s. Re-check after 60s.", volume_status)
        if not volume_state_good:
            raise RuntimeError("Timeout reached: Volume state is %s after %ss and not in UNBOUND state as expected", volume_status, WAIT_TIMEOUT)

        logger.info("Creating VSR to BIND, PUBLISH volume %s", volume_name)
        complete_by_time = (datetime.datetime.utcnow() + datetime.timedelta(
            seconds=WAIT_TIMEOUT)).isoformat(timespec='seconds') + 'Z'
        data = {
            "volumeSeriesId": self.get_volume_id(volume_name),
            "completeByTime": complete_by_time,
            "requestedOperations": ["BIND", "PUBLISH"],
            "clusterId": self.get_cluster_id(cluster_name)
            }
        response = self._api_request("POST", self.url_vsr, json_data=json.dumps(data))
        vsr_id = response['meta']['id']

        # WAit for VSR to succeed/fail
        time_now = datetime.datetime.utcnow()
        vsr_succeeded = False
        while datetime.datetime.utcnow() <= (time_now + datetime.timedelta(seconds=WAIT_TIMEOUT)):
            response = self._api_request("GET", self.url_vsr + "/" + vsr_id)
            logger.info("DEBUG - Bind, Publish VSR response data: %s", data)
            if response['volumeSeriesRequestState'] == 'SUCCEEDED':
                logger.info("DEBUG - vsr_id: %s SUCCEEDED", vsr_id)
                vsr_succeeded = True
                break
            elif response['volumeSeriesRequestState'] == 'FAILED':
                logger.info("DEBUG - vsr_id: %s FAILED", vsr_id)
                logger.info("vsr state details: %s", response)
                raise RuntimeError("VSR for BIND, PUBLISH FAILED")
            else:
                logger.info("VSR state is: %s. Sleeping for 30 seconds to wait for VSR to go to "
                            "SUCCEEDED/FAILED state", response['volumeSeriesRequestState'])
                time.sleep(30)

        if not vsr_succeeded:
            logger.info("vsr state details: %s", response)
            raise RuntimeError("TIMEOUT (%s seconds) waiting for bind & publish VSR %s to succeed." %
                            WAIT_TIMEOUT, vsr_id)
        return

    def create_protection_domain(self, encryption_algorithm="AES-256", passphrase=None):
        """ Creates a protection domain object with an encryption algorithm, if specified """

        if encryption_algorithm == "AES-256" and not passphrase:
            passphrase = str(uuid.uuid4())[:16]

        pd_name = "pd-" + str(uuid.uuid4())[:4]
        url = self.url_base + PROTECTION_DOMAIN
        logger.info("url: %s", url)
        data = dict(
            name=pd_name,
            encryptionAlgorithm=encryption_algorithm,
            encryptionPassphrase=dict(
                kind="SECRET",
                value=passphrase
            ),
            accountId=self.account_id
        )

        response = self._api_request("POST", url, json_data=json.dumps(data))
        logger.info("Protection domain %s created for account %s", response['name'], self.account_id)

        return response['meta']['id']

    def set_protection_domain(self, protection_domain_id, csp_domain_id=None):
        """ Sets a protection domain to a csp domain for an account """

        query_params = dict(protectionDomainId=protection_domain_id)
        if csp_domain_id:
            query_params['cspDomainId'] = csp_domain_id
        logger.info("Query params: %s", query_params)

        url = "%s/%s/%s" % (self.url_base + ACCOUNTS, self.account_id, PROTECTION_DOMAIN)
        response = self._api_request("POST", url, query_params=query_params)
        logger.info("Protection domain %s is set", protection_domain_id)

        return

    def set_snapshot_catalog_policy(self, protection_domain_id, csp_domain_id, overwrite=False):
        """ Sets the snapshot catalog policy for an account """

        query_params = dict(set="snapshotCatalogPolicy")
        data = dict(
            snapshotCatalogPolicy = dict(
                protectionDomainId=protection_domain_id,
                cspDomainId=csp_domain_id
            )
        )
        set_policy = False
        url = "%s/%s" % (self.url_base + ACCOUNTS, self.account_id)
        response = self._api_request("GET", url)

        try:
            domain_id = response['snapshotCatalogPolicy']['cspDomainId']
            pd = response['snapshotCatalogPolicy']['protectionDomainId']
            if overwrite:
                set_policy = True
                logger.info("Overwriting existing snapshot catalog policy: csp domain id: %s, protection domain id: %s", domain_id, pd)
            else:
                logger.info("Snapshot catalog policy already exists. csp domain id: %s, protection domain id: %s. Skip overwriting", domain_id, pd)
        except KeyError as e:
            logger.info("No existing snapshot catalog policy. Setting the specified policy")
            set_policy = True

        if set_policy:
            response = self._api_request("PATCH", url, query_params=query_params, json_data=json.dumps(data))
            logger.info("Snapshot catalog policy set:  %s", response['snapshotCatalogPolicy'])

        return

    def switch_accounts(self, account_name):
        account_id = self.find_account_id(account_name=account_name)
        self.account_id = account_id
        self.update_headers()
        logger.info("Switched to account %s", account_name)
        return

    def get_all_accounts(self):
        url = self.url_base + ACCOUNTS
        logger.info("url: %s", url)
        response = self._api_request("GET", url)
        return response

    def get_snapshots(self, volume_name=None, sort_field=None, sort_order='desc'):
        url = self.url_base + SNAPSHOTS
        query_params = {}
        if volume_name:
            query_params['volumeSeriesId'] = self.get_volume_id(volume_name)
        if sort_field:
            if sort_order == 'desc':
                query_params['sortDesc'] = sort_field
            else:
                query_params['sortAsc'] = sort_field

        response = self._api_request("GET", url, query_params=query_params)
        return response

