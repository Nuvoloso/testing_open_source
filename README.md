# testing
README

In order to run scripts in ‘testing’ repository, 

It is recommended to do this on an Ubuntu 18.04 VM in AWS (Recommended AMI: ami-702d7f08).

Steps:
1. Launch a VM in AWS (Recommended AMI: ami-702d7f08) in Oregon and zone:us-west-2c and ssh into this new EC2 VM (from your Mac).
Note: If ssh fails, you could create a new key-pair in AWS console (EC2 -> Key Pairs -> Create Key Pair). Then copy the .pem file into your ~/.ssh/ directory. Then on terminal, add it. E.g. 'ssh-add ~/.ssh/bot1awskeypair.pem'. After this, ssh to this new VM should succeed. Next steps are all run on this new EC2 VM.

2. Checkout git repository on this vm
	
	git clone https://github.com/Nuvoloso/testing.git 
   Note: You would your github username/password for this step.
	
3. Export library path:
	
	export PYTHONPATH=/home/ubuntu/testing/lib
	
4. Run the script using python3. At the top of each script, you should see an example of how to invoke it. For example:
	
	python3 testing/testingtools/deploy_fio.py --kops_cluster_name nuvodataplanecluster.k8s.local --nodes 2 --kops_state_store s3://kops-neha-nuvoloso --aws_access_key AKIAJXXXXMQ --aws_secret_access_key yISSSSSSSS6xbhCg/ --region us-west-2 --nuvo_kontroller_hostname ac7862895022711e9b5fe0acfcc322df-2138322790.us-west-2.elb.amazonaws.com

5. You should see a log file appear as soon as the test is started. It would be in the same directory as the script itself, with same name.

Note:
The 'scripts' contains scripts that test Nuvoloso solution.
The 'testingtools' directory contains scripts that are more like tools. These are little more general-purpose scripts. 
After CUM-1378 is fixed, we'll support other zones. For now, us-west-2c is the only one supported.
