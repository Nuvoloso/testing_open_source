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

VS object helper tests
"""

import json
import os.path
from unittest.mock import patch

from nuvoloso.api import obj_helper, nuvo_management

def test_vs_object():
    """Test VolumeSeriesObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/vs.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.VolumeSeriesObject(obj_json)
    assert obj is not None
    assert obj.raw() is not None
    assert obj.raw() == obj_json
    assert obj.url_type() == nuvo_management.VOLUME_SERIES
    assert obj.meta_id() == "99ac109d-8f55-4ae9-bd96-0b23483cec83"
    assert obj.meta_version() == 15
    assert obj.boundClusterId == "1bbed242-1ce6-45e6-8172-aa1e370ccb9f"
    assert obj.boundCspDomainId == "795e5d76-ee63-4366-b3d4-07bd2ab40250"
    assert obj.consistencyGroupId == "1e59174c-546a-4ba0-ae55-f43e6a3ad055"
    assert obj.volumeSeriesState == "IN_USE"
    assert obj.is_in_use()
    assert obj.is_bound()
    assert obj.is_provisioned()
    assert obj.servicePlanId == "de899da0-f146-495c-ad7d-046c0e3a540d"
    assert obj.tags == ["vs-tag1"]
    assert obj.mounted_node() == "5ec2be07-b88f-4c19-b53c-465156df1f08"
    assert obj.target_path() == '/mnt'
    assert obj.extract_storage_ids_from_messages() == ['61e4f6b9-5d90-4de5-8613-c13170da885f', 'fake-second-storage', 'fake-third-storage']
    assert obj.find_in_messages('Successfully created the LogVol')

    obj_json["mounts"] = []
    obj_json["volumeSeriesState"] = 'CONFIGURED'
    obj = obj_helper.VolumeSeriesObject(obj_json)
    assert not obj.mounted_node()
    assert obj.volumeSeriesState == "CONFIGURED"
    assert obj.is_bound()
    assert obj.is_provisioned()
    assert not obj.is_in_use()

    obj_json["mounts"] = [
        {
            "mountedNodeId": "5ec2be07-b88f-4c19-b53c-465156df1f08",
            "snapIdentifier": "not-the-HEAD"
        }
    ]
    obj_json["volumeSeriesState"] = 'PROVISIONED'
    obj = obj_helper.VolumeSeriesObject(obj_json)
    assert not obj.mounted_node()
    assert obj.volumeSeriesState == "PROVISIONED"
    assert obj.is_bound()
    assert obj.is_provisioned()
    assert not obj.is_in_use()

    obj_json["volumeSeriesState"] = 'BOUND'
    obj = obj_helper.VolumeSeriesObject(obj_json)
    assert obj.volumeSeriesState == "BOUND"
    assert obj.is_bound()
    assert not obj.is_provisioned()
    assert not obj.is_in_use()

    obj_json["volumeSeriesState"] = 'UNBOUND'
    obj = obj_helper.VolumeSeriesObject(obj_json)
    assert obj.volumeSeriesState == "UNBOUND"
    assert not obj.is_bound()
    assert not obj.is_provisioned()
    assert not obj.is_in_use()

    try:
        print(obj.foo)
    except KeyError:
        pass
    else:
        assert False, "obj.foo should throw"

    obj = obj_helper.VolumeSeriesObject()
    assert obj is not None, "empty vs is not None"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vs_object_load(mock_nuvo_mgmt):
    """Test VolumeSeriesObject loader"""
    obj = obj_helper.VolumeSeriesObject({})
    mock_nuvo_mgmt.api_request.return_value = {'meta':{'id':'VS-1', 'version':1}}
    obj.load(mock_nuvo_mgmt, obj_id='VS-1')
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/'+nuvo_management.VOLUME_SERIES+'/VS-1')
    assert obj.meta_id() == 'VS-1'
    assert obj.meta_version() == 1
    # reload
    mock_nuvo_mgmt.api_request.return_value = {'meta':{'id':'VS-1', 'version':2}}
    obj.load(mock_nuvo_mgmt, obj_id='IGNORED')
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/'+nuvo_management.VOLUME_SERIES+'/VS-1')
    assert obj.meta_id() == 'VS-1'
    assert obj.meta_version() == 2


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_vs_object_find(mock_nuvo_mgmt):
    """Test VolumeSeriesObject loader"""
    obj = obj_helper.VolumeSeriesObject({})
    mock_nuvo_mgmt.api_request.return_value = [{'meta':{'id':'vs name', 'version':1}}]
    obj.find(mock_nuvo_mgmt, 'vs name')
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/'+nuvo_management.VOLUME_SERIES)
    assert mock_nuvo_mgmt.api_request.call_args[1] == {'query_params': {"name": 'vs name'}}
    assert obj.meta_id() == 'vs name'
    assert obj.meta_version() == 1
