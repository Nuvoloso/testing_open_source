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

Contains methods for convenience
"""

import logging
import subprocess

CHECK_OUTPUT_TIMEOUT = 3600
WAIT_TIMEOUT = 600

logger = logging.getLogger(__name__)


class NuvolosoHelper(object):
    """Contains methos that are often used in scripts"""

    def __init__(self, args):
        self.args = args

    def run_check_output(self, cmd, stderr=subprocess.STDOUT, timeout=CHECK_OUTPUT_TIMEOUT,
                         encoding='UTF-8', shell=True):
        """Just runs a command and returns output"""

        try:
            logger.info("cmd: %s", cmd)
            result = subprocess.check_output(cmd, stderr=stderr, timeout=timeout, encoding=encoding,
                                             shell=shell)
            if result:
                result = result.strip()
                logger.info(result)
            return result
        except subprocess.CalledProcessError as err:
            logger.info("Failed to run cmd: %s, err: %s", cmd, err)
            raise

    def cleanup(self):
        '''Cleanup'''

        kops_del_cmd = "kops delete cluster --name " + self.args.kops_cluster_name + " --state " + \
            self.args.kops_state_store + " --yes"
        logging.info("Running cmd: %s", kops_del_cmd)
        try:
            # Delete kps cluster (containing app + clusterd)
            result = subprocess.check_output(kops_del_cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            logging.info(result)
            # TODO: remove the *sh and *yaml created by this test
        except subprocess.CalledProcessError as err:
            if err.output: logging.info(err.output)
            raise
