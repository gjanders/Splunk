#run this with splunk cmd python3 or python3
import requests
import json
import time
import sys

wait_for_seconds = 60 * 10

base_url="https://localhost:8089"

try:
    with open('/opt/splunk/.password','r') as file:
        password = file.readlines()[0].strip()
except:
    print("Unable to open password file")
    sys.exit(2)

# role with capabilities /opt/splunk/bin/splunk _internal call /services/authorization/roles -post:capabilities edit_indexer_cluster -post:capabilities list_indexer_cluster -post:name roll_buckets_automated -auth ... required
auth=('roll_buckets_automated', password)

url = base_url + "/services/cluster/manager/fixup?output_mode=json&count=0&level=replication_factor"
res = requests.get(url,auth=auth,verify=False)

dict = json.loads(res.text)
print(f"status_code={res.status_code} on url={url}")

roll_bucket_url = base_url + "/services/cluster/master/control/control/roll-hot-buckets"
resync_bucket_url = base_url + "/services/cluster/master/control/control/resync_bucket_from_peer"

current_time = round(time.time())

resync_required = False

for entry in dict['entry']:
    data_latest = entry['content']['latest']
    data_initial = entry['content']['initial']
    if data_latest['reason'].find("bucket hasn't rolled yet") != -1:
        name = entry['name']
        reason = data_latest['reason']
        print(f"bucket={name} requires role due to {reason}")
        bucket_timestamp = data_initial['timestamp']
        diff = current_time - bucket_timestamp
        if diff > wait_for_seconds*2:
            resync_required = True
        elif diff > wait_for_seconds:
            print(f'bucket={name} requires role due to {reason}, and is beyond {wait_for_seconds} seconds')
            data = { 'bucket_id': name }
            print(f'requests.post("{roll_bucket_url}", data={data}, verify=False)')
            res=requests.post(roll_bucket_url, auth=auth, data=data, verify=False)
            if res.status_code != requests.codes.ok:
                print(f'bucket={name} code={res.status_code} text={res.text}')

        # by this time we have tried to roll the buckets, so now a re-sync might be required instead
        if diff > (wait_for_seconds*2):
            url = base_url + "/services/cluster/master/buckets/" + name + "?output_mode=json"
            res = requests.get(url,auth=auth,verify=False)
            dict_buckets = json.loads(res.text)
            print(f"status_code={res.status_code} on url={url}")
            peer = list(dict_buckets['entry'][0]['content']['peers'].keys())[0]
            data = { 'bucket_id': name, 'peer': peer }
            print(f'requests.post("{resync_bucket_url}", data={data}, verify=False)')
            res=requests.post(resync_bucket_url, auth=auth, data=data, verify=False)
            if res.status_code != requests.codes.ok:
                print(f'bucket={name} code={res.status_code} text={res.text}')
            
        time.sleep(1)
