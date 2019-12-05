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

Contains methods for installing KOPs and Kubectl and et all
"""

import logging
import boto3

CHECK_OUTPUT_TIMEOUT = 360
WAIT_TIMEOUT = 600

LOGGER = logging.getLogger(__name__)

class AWS():
    """AWS helper class"""

    def __init__(self, args):
        self.args = args
        session = boto3.session.Session(profile_name=args.aws_profile_name)
        self.ec2 = session.resource('ec2', region_name=self.args.region)

    def get_instances(self, tag_name, tag_value_list):
        """Get instances based on filters passed"""

        instances = self.ec2.instances.filter(
            Filters=[{
                'Name' : tag_name,
                'Values' : tag_value_list,
            }])
        LOGGER.info("instances: %s", instances)
        return instances

    def get_instances_by_id(self, ids):
        """Get instances based on ids passed"""
        return self.ec2.instances.filter(InstanceIds=ids)

    def get_public_ips(self, private_ip_list):
        """Get list of public ips of Hosts using their private Ips"""

        list_public_ips = []
        instances = self.get_instances(
            tag_name='network-interface.addresses.private-ip-address',
            tag_value_list=private_ip_list)
        for i in instances:
            list_public_ips.append(i.public_ip_address)

        LOGGER.info("instance's public_ip_address: %s", list_public_ips)
        return list_public_ips

    def get_public_ip(self, private_ip_list):
        """Get public ip address of VM using its Private Ip"""

        instances = self.get_instances(
            tag_name='network-interface.addresses.private-ip-address',
            tag_value_list=[private_ip_list])
        LOGGER.info(instances[0].public_ip_address)
        return instances[0].public_ip_address

    def terminate_instances(self, ids, dry_run=False):
        """Terminate a list of instances"""
        LOGGER.info("terminating: %s dry_run=%s", ids, dry_run)
        self.ec2.instances.filter(InstanceIds=ids).terminate(DryRun=dry_run)
