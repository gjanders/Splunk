import requests
import json
base_url="https://localhost:8089"
url = base_url + "/services/cluster/master/buckets?filter=meets_multisite_replication_count=false&output_mode=json&count=0"
auth=('admin','changeme')
requests.get(url)
res=requests.get(url,auth=auth,verify=False)

dict = json.loads(res.text)

roll_bucket_url = base_url + "/services/cluster/master/control/control/roll-hot-buckets"
resync_bucket_url = base_url + "/services/cluster/master/control/control/resync_bucket_from_peer"
for entry in dict['entry']:
    site_count = len(entry['content']['rep_count_by_site'])
    copy_count = 2
    if site_count == 1:
        copy_count = list(entry['content']['rep_count_by_site'].values())[0]        
    name = entry['name']    
    if rep_count_by_site == 1 and copy_count < 2:
        print(f'{name} is only found on 1 site, rolling hot bucket')
        data = { 'bucket_id': name }
        print(f'requests.post({roll_bucket_url}, data={data}, verify=False)')
        res=requests.post(roll_bucket_url, auth=auth, data=data, verify=False)
        if res.status_code != requests.codes.ok:
            print(f'code={res.status_code} text={res.text}')
    else:
        print(f'{name} is found on {rep_count_by_site} sites with {copy_count}, resyncing hot bucket')
        peer = list(entry['content']['peers'].keys())[0]
        data = { 'bucket_id': name, 'peer': peer }
        print(f'requests.post({resync_bucket_url}, data={data}, verify=False)')
        res=requests.post(resync_bucket_url, auth=auth, data=data, verify=False)
        if res.status_code != requests.codes.ok:
            print(f'code={res.status_code} text={res.text}')
