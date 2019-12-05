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

SR object helper tests
"""

import json
import os.path
from unittest.mock import patch

from nuvoloso.api import obj_helper, nuvo_management

def test_sr_object():
    """Test StorageRequestObject methods"""
    data_dir = os.path.dirname(os.path.abspath(__file__))
    with open(data_dir+"/data/sr.json") as data_fp:
        obj_json = json.load(data_fp)
        obj = obj_helper.StorageRequestObject(obj_json)
    assert obj is not None, "sr is not None"
    assert obj.raw() is not None, "sr has data"
    assert obj.raw() == obj_json, "sr raw == json"
    assert obj.url_type() == nuvo_management.STORAGE_REQUESTS
    assert obj.meta_id() == "a0d82dab-2224-4c42-9da2-49a343c3a93d", "obj.meta_id() ok"
    assert obj.meta_version() == 7, "obj.meta_version() ok"
    assert obj.cspDomainId == "1a729c57-41ff-41b5-83ae-0119071fe98a", "obj.cspDomainId ok"
    assert obj.clusterId == "4e826cea-6e54-415b-a69c-b0c602c6f1d0", "obj.clusterId ok"
    assert obj.nodeId == "4ad871ec-054f-492f-a41d-c9bbd5285c27", "obj.nodeId ok"
    assert obj.cspStorageType == "Amazon gp2", "obj.cspStorageType ok"
    assert obj.systemTags == ["vsr.placement:1f1a0a1e-2398-4343-bd9d-cfb2b8e81f85"], "obj.systemTags"
    assert obj.storageRequestState == "USING", "obj.storageRequestState ok"
    assert obj.storageId == "81674950-f853-4a22-99cf-64424997e96b", "obj.storageId ok"
    assert obj.find_in_messages("sreq/block-nuvo-use-device")

    try:
        print(obj.foo)
    except KeyError:
        pass
    else:
        assert False, "obj.foo should throw"
    obj = obj_helper.StorageRequestObject()
    assert obj is not None, "empty cluster is not None"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_sr_wait_success(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.StorageRequestObject({'meta':{'id':'sr-1'}})
    assert obj is not None, "sr is initialized"
    assert obj.meta_id() == 'sr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'sr-1'}, 'storageRequestState':'ACTIVE'},\
        {'meta':{'id':'sr-1'}, 'storageRequestState':'SUCCEEDED'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/storage-requests/sr-1')
    assert obj.storageRequestState == 'SUCCEEDED', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_sr_wait_failure(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.StorageRequestObject({'meta':{'id':'sr-1'}})
    assert obj is not None, "sr is initialized"
    assert obj.meta_id() == 'sr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'sr-1'}, 'storageRequestState':'ACTIVE'},\
        {'meta':{'id':'sr-1'}, 'storageRequestState':'FAILED'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/storage-requests/sr-1')
    assert obj.storageRequestState == 'FAILED', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_sr_wait_non_terminal(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.StorageRequestObject({'meta':{'id':'sr-1'}})
    assert obj is not None, "sr is initialized"
    assert obj.meta_id() == 'sr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'sr-1'}, 'storageRequestState':'ACTIVE'},\
        {'meta':{'id':'sr-1'}, 'storageRequestState':'FOO_STATE'}\
    ]
    obj.wait(mock_nuvo_mgmt, non_terminal_states=['BAR_STATE', 'FOO_STATE'], poll_interval_secs=0)
    assert mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 2
    assert mock_nuvo_mgmt.api_request.call_args[0] == ('GET', '/api/v1/storage-requests/sr-1')
    assert obj.storageRequestState == 'FOO_STATE', "obj updated"


@patch('nuvoloso.api.nuvo_management.NuvoManagement', url_base='/api/v1/', spec=nuvo_management.NuvoManagement)
def test_sr_wait_timeout(mock_nuvo_mgmt):
    """Test invocation of wait"""
    obj = obj_helper.StorageRequestObject({'meta':{'id':'sr-1'}})
    assert obj is not None, "sr is initialized"
    assert obj.meta_id() == 'sr-1'
    mock_nuvo_mgmt.api_request.side_effect = [ \
        {'meta':{'id':'sr-1'}, 'storageRequestState':'ACTIVE'},\
        {'meta':{'id':'sr-1'}, 'storageRequestState':'STATE1'}\
    ]
    obj.wait(mock_nuvo_mgmt, poll_interval_secs=1, timeout_after_secs=-1)
    assert not mock_nuvo_mgmt.api_request.called
    assert mock_nuvo_mgmt.api_request.call_count == 0
