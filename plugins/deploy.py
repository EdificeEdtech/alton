import boto
import logging
import threading
import time
import requests

from collections import namedtuple
from will import settings
from will.plugin import WillPlugin
from show import get_ami

class TimeoutException(Exception):
    pass

EDC = namedtuple('EDC', ['environment', 'deployment', 'cluster'])

class DeployPlugin(WillPlugin):
    # To keep track of vars specific to this run of the command.
    local = threading.local()
    aws_profiles = settings.BOTO_PROFILES.split(';')

    def __init__(self):
        # TODO: Make these thread locals.
        self.autoscale = boto.connect_autoscale()
        self.ec2 = boto.connect_ec2()
        self.elb = boto.connect_elb()

    def deploy(self, message, ami_id):
        local.message = message
        edc = self._edc_for(ami_id)

        local.profile = edc.deployment

        asgs = self._asgs_for_edc(edc)

        old_asgs = self._clusters_for_asgs(asgs)

        new_asgs = {}
        for cluster in old_asgs.keys():
            new_asgs[cluster] = self._new_asg(cluster, ami_id)

        self._wait_for_in_service(new_asgs.values(), 300)

        elbs_to_monitor = []
        for cluster, asg in new_asgs.iteritems():
            self._enable_asg(cluster, asg)        
            elbs_to_monitor.append(self._elbs_for_asg(asg))

        # Wait for all instances to be in service in all ELBs
        try:
            self._wait_for_healthy_elbs(elbs_to_monitor, 600)
        except:
            for cluster, asg in new_asgs.iteritems():
                self._disable_asg(asg)

        for cluster,asg in old_asgs.iteritems():
            self._disable_asg(asg)
        # Woot! Deploy Done!

    def _edc_for(self, ami_id):
        logging.info("Looking up edc for {}".format(ami_id))
        tags = get_ami(ami_id, self.aws_profiles).tags

        cluster = None
        if 'cluster' in tags:
            cluster = tags['cluster']
        else:
            culster = tags['play']
        return EDC(tags['environment'], tags['deployment'], cluster)

    def _asgs_for_edc(self, edc):
        all_groups = self.autoscale.get_all_groups(profile_name=self.local.profile)
        for group in all_groups():
            tags = group.tags
            group_env = tags['environment']
            group_deployment = tags['deployment']
            if 'cluster' in tags:
                group_cluster = tags['cluster']
            else:
                group_cluster = tags['play']

            group_edc = EDC(group_env, group_deployment, group_cluster)
                         
            if group_edc == edc:
                yield group

    def _clusters_for_asgs(self, asgs):
        # TODO: Can we cache this and do it less often?
        # response = request.get("http://admin-edx-hammer.edx.org/us-east-1/cluster/list.json")
        response = request.get(self.cluster_list_url)
        cluster_json = response.json()

        relevant_clusters = {}
        for cluster in cluster_json:
            for asg in cluster['autoScalingGroups']:
                if asg in asgs:
                    relevant_clusters[cluster['cluster']] = cluster['autoScalingGroups']
                    # A cluster have multiple relevant ASGs. We don't need to check them all.
                    break

        return relevant_clusters

    def _new_asg(self, cluster, ami_id):
        'curl -d "name=helloworld-example&imageId=ami-40788629&trafficAllowed=false&checkHealth=true" http://asgardprod/us-east-1/cluster/createNextGroup'
        payload = {
            "name": cluster,
            "imageId": ami_id,
            "trafficAllowed": False,
            "checkHealth": True,
        }
        response = requests.post(self.new_asg_url, data=payload)

        self._wait_for_task_completion(response.url, 300)
       
        # Potential Race condition if multiple people are making ASGs for the same cluster
        # Return the name of the new asg
        return self._asgs_for_cluster(cluster)[-1]

    def _wait_for_in_service(self, all_asgs, timeout):
        time_left = timeout
        asgs_left_to_check = list(all_asgs)
        while time_left > 0:
            asgs = self.autoscale.get_all_groups(asgs_left_to_check)
            for asg in asgs:
                all_healthy = True
                for instance in asg.instances:
                    if instance.health_status != 'Healthy' and instance.lifecycle_state != 'InService':
                        # Instance is  not ready.
                        all_healthy = False
                        break

                if all_healthy:
                    # Then all are healthy we can stop checking this.
                    asgs_left_to_check.remove(asg.name)
            time.sleep(1)
            time_left -= 1
        raise TimeoutException("Some instances in the following ASGs never became healthy: {}".format(asgs_left))

    def _enable_asg(self, cluster, asg):
        'curl -d "name=helloworld-example-v004" http://asgardprod/us-east-1/cluster/activate'
        payload = { "name": asg }
        response = requests.post(self.asg_activate_url, data=payload)
        task_url = response.url

        self._wait_for_task_completion(task_url, 300)

    def _elbs_for_asg(self, asg):
        """
        curl http://admin-edx-hammer.edx.org/us-east-1/autoScaling/show/loadtest-edx-CommonClusterServerAsGroup-V9J31ZEID5C8-v001.json
        """
        response = requests.get(self.asg_info_url.format(asg))
        elbs = response.json()['group']['loadBalancerNames']
        return elbs

    def _asgs_for_cluster(self, cluster):
        """
        http://admin-edx-hammer.edx.org/us-east-1/cluster/show/loadtest-edx-notes.json
        """
        response = requests.get(self.cluster_info_url.format(asg))
        asgs = response.json()
        asg_names = map(lambda x: x['autoScalingGroupName'], asgs)
        return asg_names

    def _wait_for_healthy_elbs(self, elbs_to_monitor, timeout):
        time_left = timeout
        elbs_left = list(elbs_to_monitor)
        while time_left > 0:
            elbs = self.elb.get_all_load_balancers(elbs_to_monitor)
            for elb in elbs:
                all_healthy = True
                for instance in elb.get_instance_health():
                    if instance.state != 'InService':
                        all_healthy = False
                        break

                if all_healthy:
                    elbs_left.remove(elb.name)
            time.sleep(1)
            time_left -= 1

        raise TimeoutException("The following ELBs never became healthy: {}".format(elbs_left))

    def _disable_asg(self, asg):
        """
        curl -d "name=helloworld-example-v004" http://asgardprod/us-east-1/cluster/deactivate
        """
        payload = { "name": asg }
        response = requests.post(self.asg_deactivate_url, data=payload)
        task_url = response.url
        
        self._wait_for_task_completion(task_url, 300)
       
    def _wait_for_task_completion(self, task_url, timeout):
        time_left = timeout
        while time_left > 0:
            response = requests.get(task_url)
            status = response.json()['status']
            if status == 'completed'
                return
            time.sleep(1)
            time_left -= 1

        raise TimeoutException("Timedout while waiting for task {}".format(task_url))