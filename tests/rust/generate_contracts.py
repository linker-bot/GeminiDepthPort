"""Regenerate synthetic migration fixtures from the frozen Python behavior oracle."""
import copy
import json
from pathlib import Path
import sys
BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE), str(BASE / 'tests')]
import legacy_support
import configuration as cfg
import calibration
from test_configuration_v2 import fixture, confirmed
from test_calibration import evidence

def main():
    cases = []
    def add(name, data, draft=True):
        try:
            cfg.validate_inventory(data, draft=draft)
            code = None
        except cfg.ConfigurationError as exc:
            code = exc.code
        cases.append(dict(name=name, data=data, draft=draft, error=code))
    d=fixture()
    add('three draft',d)
    add('three confirmed',confirmed(d),False)
    add('unconfirmed strict',d,False)
    for count in (0,1,2):
        value=fixture()
        for camera in value['cameras'][count:]:camera['enabled']=False
        add(f'{count} enabled',value)
        add(f'{count} enabled strict',confirmed(value),False)
    mutations=[(['extra'],True),(['ros_domain_id'],True),(['ros_domain_id'],102),
        (['startup_timeout_seconds'],29),(['communication'],{'profile':'other'}),
        (['cameras',0,'namespace'],'/'),(['cameras',0,'namespace'],'/a/'),
        (['cameras',0,'namespace'],'/9invalid'),(['cameras',1,'namespace'],'/camera_a/child'),
        (['cameras',0,'frame_prefix'],'9invalid'),(['cameras',0,'profile'],'unknown'),
        (['cameras',0,'enabled'],1),(['cameras',0,'serial_number'],''),
        (['cameras',0,'serial_number'],'UNBOUND'),(['cameras',1,'serial_number'],'SYNTHETIC_0'),
        (['cameras',0,'timestamp_source'],''),(['cameras',0,'depth_unit'],'m'),
        (['cameras',0,'parameters','color_width'],3),(['cameras',0,'parameters','color_width'],True),
        (['cameras',0,'parameters','color_fps'],0),(['cameras',0,'parameters','enable_color'],1),
        (['cameras',0,'parameters','color_format'],'RGB'),(['cameras',0,'parameters','depth_format'],'ANY'),
        (['cameras',0,'parameters','depth_registration'],True),
        (['cameras',0,'parameters','color_qos'],'RELIABLE'),
        (['cameras',0,'parameters','color_qos'],None),
        (['cameras',0,'parameters','enable_color_undistortion'],True),
        (['cameras',0,'parameters','enable_point_cloud'],True),
        (['cameras',0,'parameters','serial_number'],'managed'),
        (['cameras',0,'health','sample_seconds'],1),
        (['cameras',0,'health','min_fps_ratio'],0),
        (['cameras',0,'health','freshness_seconds'],True),
        (['cameras',0,'verification'],{}),(['cameras',0,'profiles_verified'],True)]
    for path,value in mutations:
        data=fixture();target=data
        for key in path[:-1]:target=target[key]
        target[path[-1]]=value
        add('.'.join(map(str,path))+':'+repr(value),data)
    legacy=json.loads((BASE/'tests/fixtures/inventory.v1.json').read_text())
    add('legacy draft',legacy)
    add('legacy strict',legacy,False)
    migrated=cfg.migrate_legacy(legacy)
    add('migrated draft',migrated)
    special=fixture();special['cameras'][0]['calibration_revision']='标定😀';special['cameras'][0]['parameters']['color_exposure']=1e-6
    add('unicode and scientific',confirmed(special),False)
    requests=[]
    for data in (fixture(),special):
        request=calibration.capture_request(data,'a'*32)
        observations=evidence(request)
        # Exercise Python's scientific notation at a ROS helper boundary.
        next(iter(observations['cameras'].values()))['depth_to_color']['translation'][0]=1e-6
        requests.append(dict(data=data,generation='a'*32,request=request,observations=observations,
            snapshot=calibration.build_snapshot(data,'a'*32,observations)))
    result=dict(cases=cases,legacy=legacy,migrated=migrated,requests=requests)
    (BASE/'tests/rust/contracts.json').write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n')
    print(f'Generated {len(cases)} synthetic validation cases and {len(requests)} calibration cases.')
if __name__=='__main__':main()
