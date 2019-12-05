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

Contains methods for ssh, scp and executing cmds on remote VM.
For example, on host VM of nuvo process
"""

import logging
import os
import select
import time
import paramiko

from paramiko import SSHClient
from scp import SCPClient

CHECK_OUTPUT_TIMEOUT = 360
WAIT_TIMEOUT = 600

logger = logging.getLogger(__name__)


class ConnectSSH(object):
    """Connects and runs commands on remote VM"""

    def __init__(self, args):
        self.args = args
        self.ssh = paramiko.SSHClient()

    def ssh_connect(self, hostname, port=22, username=None, password=None):
        """ssh connection"""

        i = 1
        while True:
            logger.info("Trying to connect to %s (%i / 30)", hostname, i)
            try:
                self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.ssh.connect(hostname=hostname, port=port, username=username, password=password, timeout=10)
                break
            except paramiko.AuthenticationException:
                logger.error("Authentication failed when connecting to %s", hostname)
                raise
            except:
                logger.info("Could not SSH to %s. Retrying...", hostname)
                i = i + 1
                time.sleep(3)

            # If we could not connect within retry limit
            if i >= 30:
                raise Exception("Could not connect to %s. Hit max retries. Giving up" % hostname)

    def execute_cmd_remote(self, hostname, cmd_to_execute, port=22, username='ubuntu',
                           password=None):
        """Execute a command on a remote host, for example on a host vm of nuvo"""

        std_out = None
        logger.info("cmd_to_execute: %s on host: %s", cmd_to_execute, hostname)
        self.ssh_connect(hostname, port, username, password)
        try:
            stdin, stdout, stderr = self.ssh.exec_command(cmd_to_execute)
            logger.info("stdin: %s\n, stdout: %s\n, stderr: %s\n", stdin, stdout, stderr)
            std_out = stdout.read().decode("utf-8")
            if std_out: logger.info("std_out: %s", std_out)
            return std_out
        except:
            raise
        finally:
            self.ssh.close()
            logger.info("ssh closed")

    def scp_copy_file(self, hostname, src_path, dst_path, port=22, username='ubuntu', password=None):
        """Scp file from src to destination"""

        logger.info("SCP Copying file: %s from %s to path: %s", src_path, hostname, dst_path)

        self.ssh_connect(hostname, port, username, password)
        scp = SCPClient(self.ssh.get_transport(), sanitize=lambda x: x)
        scp.get(src_path, dst_path)
        logger.info("scp done")
        scp.close()

    def scp_copy_to_node(self, hostname, src_path, dst_path, port=22, username='ubuntu', password=None):
        """Scp file from localhost to destination"""

        logger.info("SCP Copying file: %s from %s to path: %s", src_path, hostname, dst_path)

        self.ssh_connect(hostname, port, username, password)
        scp = SCPClient(self.ssh.get_transport(), sanitize=lambda x: x)
        scp.put(src_path, dst_path)
        logger.info("scp done")
        scp.close()
