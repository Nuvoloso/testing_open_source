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

Storage object helper tests
"""

import json
import os.path
from unittest.mock import patch

from nuvoloso.api import obj_helper, nuvo_management

def test_storage_object():
    """Test StorageObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/storage.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.StorageObject(obj_json)
    assert obj is not None
    assert obj.raw() is not None
    assert obj.raw() == obj_json
    assert obj.url_type() == nuvo_management.STORAGE
    assert obj.meta_id() == "00e1c978-fbdf-431b-af68-02d8c5e3905c"
    assert obj.meta_version() == 8
    assert obj.clusterId == "1bbed242-1ce6-45e6-8172-aa1e370ccb9f"
    assert obj.cspDomainId == "795e5d76-ee63-4366-b3d4-07bd2ab40250"
    assert obj.cspStorageType == "Amazon gp2"
    assert obj.attachment_state() == "ATTACHED"
    assert obj.device_state() == "OPEN"
    assert obj.media_state() == "FORMATTED"
    assert obj.provisioned_state() == "PROVISIONED"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_storage_object_load(mock_nuvo_mgmt):
    """Test StorageObject loader"""
    obj = obj_helper.StorageObject()
    mock_nuvo_mgmt.api_request.return_value = {'meta':{'id':'STORAGE-1', 'version':1}}
    obj.load(mock_nuvo_mgmt, obj_id='STORAGE-1')
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/'+nuvo_management.STORAGE+'/STORAGE-1')
    assert obj.meta_id() == 'STORAGE-1'
    assert obj.meta_version() == 1
    # reload
    mock_nuvo_mgmt.api_request.return_value = {'meta':{'id':'STORAGE-1', 'version':2}}
    obj.load(mock_nuvo_mgmt, obj_id='IGNORED')
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/'+nuvo_management.STORAGE+'/STORAGE-1')
    assert obj.meta_id() == 'STORAGE-1'
    assert obj.meta_version() == 2
