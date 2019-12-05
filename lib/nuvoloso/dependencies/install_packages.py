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
import os
import subprocess

from pathlib import Path


CHECK_OUTPUT_TIMEOUT = 360
WAIT_TIMEOUT = 600

logger = logging.getLogger(__name__)


class InstallPackages(object):
    """Deploys Nuvo Control Plane (aka 'Kontroller') inside a new KOPS cluster"""

    @staticmethod
    def get_home():
        """Returns home dir of host"""
        return os.environ.get('HOME') if os.environ.get('HOME') else "~"

    @staticmethod
    def apt_get_update():
        """Run apt-get update to update the packages"""

        cmd_apt_get_update = "sudo apt-get update"
        try:
            result = subprocess.check_output(cmd_apt_get_update, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result: logger.info(result)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            raise

    @staticmethod
    def install_kops():
        """Install KOPS"""

        cmd_wget_kops = "wget -O kops https://github.com/kubernetes/kops/releases/download" \
            "/$(curl -s https://api.github.com/repos/kubernetes/kops/releases/latest | " \
            "grep tag_name | cut -d '\"' -f 4)/kops-linux-amd64"
        cmd_chmod_mv = "chmod +x ./kops ; sudo mv ./kops /usr/local/bin/"
        try:
            result = subprocess.check_output("which kops", stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and result.strip()[-4:] == "kops":
                logger.info(result)
                logger.info("kops is already installed")
            else:
                raise Exception("'which kops' output is unexpected")
                
        except subprocess.CalledProcessError as err:
            if not err.output:
                # which command yielded nothing. That means kops is not installed.
                result = subprocess.check_output(cmd_wget_kops, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
                result = subprocess.check_output(cmd_chmod_mv, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
                result = subprocess.check_output("which kops", stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and result.strip()[-4:] == "kops":
                    logger.info(result)
                else:
                    logger.info(result)
                    raise Exception("Failed to install kops")
            else:
                logger.info(err.output)
                raise

    @staticmethod
    def install_kubectl():
        """Install Kubectl"""

        cmd_wget_kubectl = "curl -LO https://storage.googleapis.com/" \
            "kubernetes-release/release/$(curl -s https://storage.googleapis.com/" \
            "kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl"
        cmd_chmod_mv = "chmod +x ./kubectl ; sudo mv ./kubectl /usr/local/bin/kubectl"
        try:
            result = subprocess.check_output("which kubectl", stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and result.strip()[-7:] == "kubectl":
                logger.info(result)
                logger.info("kubectl is already installed")
            else:
                raise Exception("'which kubectl' output is unexpected")
        except subprocess.CalledProcessError as err:
            if not err.output:
                result = subprocess.check_output(cmd_wget_kubectl, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
                result = subprocess.check_output(cmd_chmod_mv, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
                result = subprocess.check_output("which kubectl", stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and result.strip()[-7:] == "kubectl":
                    logger.info(result)
                else:
                    raise Exception("Failed to install kubectl")
            else:
                logger.info(err.output)
                raise

    @staticmethod
    def install_awscli():
        """Install AWS CLI"""

        cmd_install_awscli = "sudo apt -y install awscli"
        cmd_check_version = "aws --version"
        try:
            result = subprocess.check_output(cmd_check_version, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result and "not found" not in result and "botocore" in result:
                logger.info(result)
                logger.info("aws-cli is already installed")
            else:
                raise Exception("'aws --version' output is unexpected")
        except subprocess.CalledProcessError as err:
            if err.returncode == 127:
                if err.output: logger.info(err.output)
                result = subprocess.check_output(cmd_install_awscli, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
                result = subprocess.check_output(cmd_check_version, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result and "botocore" in result:
                    logger.info(result)
                else:
                    raise Exception("Failed to install awscli")
            else:
                raise

    @staticmethod
    def configure_aws(args):
        """Configure AWS"""

        cmd_mkdir = "mkdir " + InstallPackages.get_home() + "/.aws"
        cmd_touch_config = "touch " + InstallPackages.get_home() + "/.aws/config"
        cmd_touch_credentials = "touch " + InstallPackages.get_home() + "/.aws/credentials"

        cmd_access_key = "aws configure set aws_access_key_id " + args.aws_access_key
        cmd_secret_access_key = "aws configure set aws_secret_access_key " + \
            args.aws_secret_access_key
        cmd_set_aws_region = "aws configure set default.region " + args.region
        aws_cred_file = Path(InstallPackages.get_home() + "/.aws/credentials")
        try:
            if aws_cred_file.is_file():
                logger.info("aws credentials file (.aws/credentials) exists. Skipping aws "
                            "configure.")
            else:
                if not os.path.isdir(Path(InstallPackages.get_home() + "/.aws")):
                    result = subprocess.check_output(cmd_mkdir, stderr=subprocess.STDOUT,
                                                     timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                     shell=True)
                    if result: logger.info(result)
                result = subprocess.check_output(cmd_touch_config, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_touch_credentials, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_access_key, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_secret_access_key, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                result = subprocess.check_output(cmd_set_aws_region, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            logger.info("Failed to configure aws.")
            raise

    @staticmethod
    def generate_sshkeypair():
        """ssh-keygen -t rsa"""

        cmd_generate_sshkeypair = "yes " + InstallPackages.get_home() + \
            "/.ssh/id_rsa |ssh-keygen -q -t rsa -N '' >/dev/null"
        try:
            if os.path.isfile(InstallPackages.get_home() + "/.ssh/id_rsa"):
                logger.info("ssh keypair seems to exist already. We'll use it instead")
                return
            else:
                logger.info("Generating ssh key pair now")
                result = subprocess.check_output(cmd_generate_sshkeypair, stderr=subprocess.STDOUT,
                                                 timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                                 shell=True)
                if result: logger.info(result)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            raise

    @staticmethod
    def _run_check_output(cmd, error_code_one_ok=False):
        '''runs the command
        error_code_one_ok can be set to ignore just error code 1 for commands like grep

        returns empty string if error_code_one_ok is set to True and command fails with error code 1'''

        try:
            logger.info("Running cmd: %s ...", cmd)
            result = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                             timeout=CHECK_OUTPUT_TIMEOUT, encoding='UTF-8',
                                             shell=True)
            if result:
                logger.info(result)
                return result
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            logger.info("Failed to run cmd: %s", cmd)
            if error_code_one_ok and err.returncode == 1:
                return ''
            raise

    @staticmethod
    def _install_pip3_now():
        """Install pip3"""

        cmd_list = [
            'sudo apt update',
            'sudo apt install -y python3-pip',
            'pip3 --version'
        ]
        for cmd in cmd_list:
            InstallPackages._run_check_output(cmd)

    @staticmethod
    def install_pip3():
        """install pip3 if not installed already"""

        try:
            cmd = 'pip3 --version'
            InstallPackages._run_check_output(cmd)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            InstallPackages._install_pip3_now()

    @staticmethod
    def install_boto3():
        """Installs boto (AWS SDK for Python3)"""

        cmd = 'dpkg -l | egrep python3-boto'
        result = InstallPackages._run_check_output(cmd, True)

        if result != '':
            logger.info('python3-boto already installed')
            return

        cmd_list = [
            'sudo apt update',
            'sudo apt-get install python3-boto',
        ]

        for cmd in cmd_list:
            logger.info('Install via cmd %s', cmd)
            InstallPackages._run_check_output(cmd)

    @staticmethod
    def install_paramiko():
        """Installs paramiko"""

        cmd = 'pip3 list | egrep paramiko'
        result = InstallPackages._run_check_output(cmd, True)

        if result != '':
            logger.info('paramiko already installed')
            return

        cmd ='sudo apt install python3-paramiko'
        logger.info('Install via cmd %s', cmd)
        InstallPackages._run_check_output(cmd)

    @staticmethod
    def install_scp():
        """install scp module"""

        cmd = 'dpkg -l | egrep python3-scp'
        result = InstallPackages._run_check_output(cmd, True)

        if result != '':
            logger.info('scp already installed')
            return

        try:
            cmd_list = [
                'sudo apt update',
                'sudo apt-get install python3-scp',
            ]

            for cmd in cmd_list:
                logger.info('Install via cmd %s', cmd)
                InstallPackages._run_check_output(cmd)
        except subprocess.CalledProcessError as err:
            if err.output: logger.info(err.output)
            logger.info("XXX scp installation cored. Ignoring for now..")
