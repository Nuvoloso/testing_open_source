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

Object helpers
"""

import datetime
import json
import re
import time
import uuid

from .nuvo_management import CLUSTERS, NODES, STORAGE, STORAGE_REQUESTS, VOLUME_SERIES, VOL_SERIES_REQUESTS

class KontrollerObject():
    """A helper for a generic kontroller object"""

    def __init__(self, data=None, url_type=None, msg_attr='messages'):
        self._data = data
        if not self._data:
            self._data = {}
        self._url_type = url_type
        self._msg_attr = msg_attr

    def url_type(self):
        """Return the object type"""
        return self._url_type

    def raw(self):
        """returns the raw data"""
        return self._data

    def load(self, nuvo_mgmt, obj_id=None):
        """load or reload the object if the obj_id and url_type are known"""
        if not self._url_type:
            raise ValueError("url_type is not known")
        if "meta" in self._data:
            obj_id = self._data["meta"]["id"]
        elif not obj_id:
            raise ValueError("object id is not known")
        url = "%s%s/%s" % (nuvo_mgmt.url_base, self._url_type, obj_id)
        self._data = nuvo_mgmt.api_request("GET", url)

    def query(self, nuvo_mgmt, query_params):
        """Query for this object type"""
        url = "{}{}".format(nuvo_mgmt.url_base, self._url_type)
        return nuvo_mgmt.api_request("GET", url, query_params=query_params)

    def find_in_messages(self, substring):
        """Find a substring in messages"""
        for msg in self._data[self._msg_attr]:
            if str(msg["message"]).find(substring) >= 0:
                return True
        return False

    def meta_id(self):
        """returns the id of the object"""
        return self._data["meta"]["id"]

    def meta_version(self):
        """returns the version of the object"""
        return self._data["meta"]["version"]

    def __getattr__(self, name):
        return self._data[name]


class KontrollerObjectWithService(KontrollerObject):
    """A helper for a generic kontroller object with a service property"""

    def _service(self):
        """provides an empty service data structure if missing"""
        if "service" in self._data and isinstance(self._data["service"], dict):
            return self._data['service']
        return {'serviceLocator':'', 'messages':[], 'state':"UNKNOWN", 'serviceType':''}

    def service_pod(self):
        """returns the name of the service pod"""
        return self._service()["serviceLocator"]

    def service_messages(self):
        """returns the messages of the service"""
        return self._service()["messages"]

    def service_state(self):
        """returns the state of the service"""
        return self._service()["state"]

    def service_type(self):
        """returns the type of service"""
        return self._service()["serviceType"]

    def service_cooked_state(self):
        """returns the cooked state"""
        state = self.state
        if state == "MANAGED":
            state += "/" + self.service_state()
        return state

SR_TERMINAL_STATES = ['SUCCEEDED', 'FAILED']

class StorageRequestObject(KontrollerObject):
    """A StorageRequestObject object helper"""

    def __init__(self, data=None):
        super().__init__(data, STORAGE_REQUESTS, 'requestMessages')

    def query(self, nuvo_mgmt, query_params):
        """Query for storage requests"""
        ret_list = []
        for obj in super().query(nuvo_mgmt, query_params):
            ret_list.append(StorageRequestObject(obj))
        return ret_list

    def wait(self, nuvo_mgmt, non_terminal_states=None, timeout_after_secs=180000, poll_interval_secs=5):
        """Wait for the SR to terminate or reach one of a set of non-terminal states or the timeout limit is reached"""
        if self.meta is None:
            raise ValueError("SR has not been loaded")
        if non_terminal_states is not None:
            if not isinstance(non_terminal_states, list):
                raise ValueError("non_terminal_states must be a list")

        max_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=timeout_after_secs)
        while datetime.datetime.utcnow() <= max_time:
            self.load(nuvo_mgmt)
            if self.storageRequestState in SR_TERMINAL_STATES:
                return
            if non_terminal_states is not None and self.storageRequestState in list(non_terminal_states):
                return
            time.sleep(poll_interval_secs)


VS_VSR_OPS = ['CREATE', 'BIND', 'MOUNT', 'ATTACH_FS', 'UNMOUNT', 'DETACH_FS', 'UNMOUNT', 'UNBIND', 'VOL_SNAPSHOT_CREATE', 'DELETE']
VS_OTHER_OPS = ['CG_SNAPSHOT_CREATE']
VSR_TERMINAL_STATES = ['SUCCEEDED', 'FAILED', 'CANCELED']

class VolumeSeriesRequestObject(KontrollerObject):
    """A VolumeSeriesRequest object helper"""

    def __init__(self, data=None):
        super().__init__(data, VOL_SERIES_REQUESTS, 'requestMessages')

    def query(self, nuvo_mgmt, query_params):
        """Query for volume series requests"""
        ret_list = []
        for obj in super().query(nuvo_mgmt, query_params):
            ret_list.append(VolumeSeriesRequestObject(obj))
        return ret_list

    # pylint: disable=too-many-arguments,too-many-locals
    def assemble(self, ops, account_id=None, vol_size_gib=0, volume_name=None, vol_name_prefix="nuvoauto-vol-", service_plan_id=None, \
                vol_id=None, cluster_id=None, node_id=None, target_path=None, fs_type=None, vol_tags=None, \
                cg_id=None, timeout_after_secs=3600):
        """Assemble the data for a VolumeSeriesRequest to perform the specified operations"""
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
        data = {}
        data["requestedOperations"] = ops
        data["completeByTime"] = (datetime.datetime.utcnow() + datetime.timedelta(seconds=timeout_after_secs)).isoformat(timespec='seconds') + 'Z'
        if 'CREATE' in ops:
            if vol_size_gib <= 0:
                raise ValueError("CREATE requires vol_size_gib")
            if volume_name is None and vol_name_prefix is None:
                raise ValueError("CREATE requires volume_name or vol_name_prefix")
            if service_plan_id is None:
                raise ValueError("CREATE requires service_plan_id")
            if account_id is None:
                raise ValueError("CREATE requires account_id")
            if volume_name is None:
                volume_name = vol_name_prefix + str(uuid.uuid4())[:5]
            cspec = {}
            cspec["accountId"] = account_id
            cspec["servicePlanId"] = service_plan_id
            cspec["name"] = volume_name
            cspec["sizeBytes"] = int(vol_size_gib) << 30 # convert to bytes
            if vol_tags is not None:
                if not isinstance(vol_tags, list):
                    raise ValueError("vol_tags must be an array")
                if vol_tags:
                    cspec["tags"] = vol_tags
            data["volumeSeriesCreateSpec"] = cspec
        elif set(ops) & set(VS_VSR_OPS):
            if vol_id is None:
                raise ValueError("vol_id is required")
            data["volumeSeriesId"] = vol_id
        elif set(ops).issubset(set(VS_OTHER_OPS)):
            pass
        else:
            raise ValueError("Other ops not yet supported")

        if 'BIND' in ops:
            if cluster_id is None:
                raise ValueError("BIND requires cluster_id")
        if 'MOUNT' in ops:
            if node_id is None:
                raise ValueError("MOUNT requires node_id")
        if 'ATTACH_FS' in ops:
            if target_path is None:
                raise ValueError("ATTACH_FS requires target_path")
            data["targetPath"] = target_path
            if fs_type is not None:
                data["fsType"] = fs_type
        if 'DETACH_FS' in ops:
            if target_path is None:
                raise ValueError("DETACH_FS requires target_path")
            data["targetPath"] = target_path
        if 'UNMOUNT' in ops:
            if node_id is None:
                raise ValueError("UNMOUNT requires node_id")
        if 'PUBLISH' in ops:
            if fs_type is not None:
                data["fsType"] = fs_type
        if 'CG_SNAPSHOT_CREATE' in ops:
            if cg_id is None:
                raise ValueError("CG_SNAPSHOT_CREATE requires cg_id")
            if cluster_id is None:
                raise ValueError("CG_SNAPSHOT_CREATE requires cluster_id")
            data['consistencyGroupId'] = cg_id
        if cluster_id is not None:
            data["clusterId"] = cluster_id
        if node_id is not None:
            data["nodeId"] = node_id
        self._data = data

    def create(self, nuvo_mgmt):
        """Create the VolumeSeriesRequest object from assembled data"""
        self._data = nuvo_mgmt.api_request("POST", "{}{}".format(nuvo_mgmt.url_base, self._url_type), json_data=json.dumps(self._data))

    def is_terminated(self):
        """Check if in a terminal state"""
        return self.volumeSeriesRequestState in VSR_TERMINAL_STATES

    def wait(self, nuvo_mgmt, non_terminal_states=None, timeout_after_secs=180000, poll_interval_secs=5):
        """Wait for the VSR to terminate or reach one of a set of non-terminal states or the timeout limit is reached"""
        if self.meta is None:
            raise ValueError("VSR has not been loaded")
        if non_terminal_states is not None:
            if not isinstance(non_terminal_states, list):
                raise ValueError("non_terminal_states must be a list")

        max_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=timeout_after_secs)
        while datetime.datetime.utcnow() <= max_time:
            self.load(nuvo_mgmt)
            if self.is_terminated():
                return
            if non_terminal_states is not None and self.volumeSeriesRequestState in list(non_terminal_states):
                return
            time.sleep(poll_interval_secs)

    def run(self, nuvo_mgmt, **kwargs):
        """Create and wait on a VSR"""
        self.create(nuvo_mgmt)
        self.wait(nuvo_mgmt, **kwargs)


class NodeObject(KontrollerObjectWithService):
    """A Node object helper"""

    def __init__(self, data=None):
        super().__init__(data, NODES)

    def _node_attributes(self):
        """provides an empty node attributes data structure if missing"""
        if "nodeAttributes" in self._data and isinstance(self._data["nodeAttributes"], dict):
            return self._data['nodeAttributes']
        return {'InstanceName':{"value":""}, 'Hostname':{"value":""}, 'public-hostname':{"value":""}}

    def aws_instance(self):
        """returns the name of the aws instance"""
        return self._node_attributes()["InstanceName"]["value"]

    def local_hostname(self):
        """returns the node local hostname"""
        return self._node_attributes()["Hostname"]["value"]

    def public_hostname(self):
        """returns the node public hostname"""
        return self._node_attributes()["public-hostname"]["value"]

    def query(self, nuvo_mgmt, query_params):
        """Query for volume series requests"""
        ret_list = []
        for obj in super().query(nuvo_mgmt, query_params):
            ret_list.append(NodeObject(obj))
        return ret_list


class ClusterObject(KontrollerObjectWithService):
    """A Cluster object helper"""

    def __init__(self, data=None):
        super().__init__(data, CLUSTERS)


class StorageObject(KontrollerObject):
    """A Storage object helper"""

    def __init__(self, data=None):
        super().__init__(data, STORAGE)

    def _storage_state(self):
        """provides an empty storage state data structure if missing"""
        if "storageState" in self._data and isinstance(self._data["storageState"], dict):
            return self._data['storageState']
        return {'attachmentState':"", 'deviceState':"", 'provisionedState':"", 'mediaState':""}

    def attachment_state(self):
        """returns the attachment state"""
        return self._storage_state()["attachmentState"]

    def device_state(self):
        """returns the device state"""
        return self._storage_state()["deviceState"]

    def media_state(self):
        """returns the media state"""
        return self._storage_state()["mediaState"]

    def provisioned_state(self):
        """returns the provisioned state"""
        return self._storage_state()["provisionedState"]


class VolumeSeriesObject(KontrollerObject):
    """A VolumeSeries object helper"""

    re_storageParcels = re.compile("storageParcels:")
    re_storageIdPattern = re.compile(r" S\[([^\]]*)\]")

    def __init__(self, data=None):
        super().__init__(data, VOLUME_SERIES)

    def find(self, nuvo_mgmt, name):
        """Search for the volume series by name"""
        objects = self.query(nuvo_mgmt, {"name": name})
        if objects:
            self._data = objects[0]
        else:
            raise RuntimeError("volume series '{}' not found".format(name))

    def vol_vsrs(self, nuvo_mgmt, is_terminated=None):
        """Search for volume series requests involving this volume series"""
        query_params = {'volumeSeriesId': self.meta_id()}
        if is_terminated is not None:
            query_params["isTerminated"] = is_terminated
        return VolumeSeriesRequestObject({}).query(nuvo_mgmt, query_params)

    def mounted_node(self):
        """Returns the nodeId if mounted"""
        if self.volumeSeriesState == "IN_USE":
            for mount in self.mounts:
                if mount["snapIdentifier"] == "HEAD":
                    return mount["mountedNodeId"]
        return None

    def target_path(self):
        """Extract the target path if set"""
        for tag in self.systemTags:
            if tag.startswith('volume.cluster.fsAttached:'):
                return tag.split(':', 2)[1]
        return None

    def is_in_use(self):
        """returns true if the volume is in use"""
        return self.volumeSeriesState == "IN_USE"

    def is_provisioned(self):
        """Returns true if storage is associated with the volume"""
        if self.volumeSeriesState in ['IN_USE', 'CONFIGURED', 'PROVISIONED']:
            return True
        return False

    def is_bound(self):
        """Returns true if the volume is bound"""
        if self.volumeSeriesState in ['IN_USE', 'CONFIGURED', 'PROVISIONED', 'BOUND']:
            return True
        return False

    def extract_storage_ids_from_messages(self):
        """Extract storage object identifiers from messages"""
        sids = {}
        for msg in self.messages:
            message = msg["message"]
            if not VolumeSeriesObject.re_storageParcels.search(message):
                continue
            for match in VolumeSeriesObject.re_storageIdPattern.finditer(message):
                sids[match.group(1)] = 1
        return list(sids.keys())
