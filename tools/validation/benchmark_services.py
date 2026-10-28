#!/usr/bin/env python3
"""Private same-image reference/official versus Rust benchmark. Default is plan only.

Not an official-driver or pure-language comparison. Execute only after all
camera tests release devices. Copies only explicit inputs, never their parent.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]

OFFICIAL_DISABLED_FLAGS = (
    'depth_registration', 'enable_depth_scale', 'enable_point_cloud',
    'enable_colored_point_cloud', 'enable_ir', 'enable_left_ir', 'enable_right_ir',
    'enable_accel', 'enable_gyro', 'enable_sync_output_accel_gyro',
    'enable_decimation_filter', 'enable_threshold_filter', 'enable_hdr_merge',
    'enable_sequence_id_filter', 'enable_spatial_filter', 'enable_temporal_filter',
    'enable_hole_filling_filter', 'enable_noise_removal_filter', 'enumerate_net_device',
)

OFFICIAL_DIFFERENCES = (
    'Official initialization may write device exposure, white-balance, laser and hardware-filter properties; Rust caller does not perform those explicit writes.',
    'Official host-clock synchronization timer may write device clock state; Rust caller does not start that timer.',
    'Official explicitly enables SDK frame sync; Rust retains the SDK constructor enable attempt.',
    'Official publishes its own Image/CameraInfo/static-TF implementation and does not use the shared ROS C ABI bridge.',
    'Rust constructs frame/pair metadata extensions even for the wire-only consumer; official has different optional metadata channels.',
    'Rust applies a device-clock 20ms pair guard and supervised readiness/status work; official has its own SDK/driver queues and no equivalent caller pair guard.',
    'Internal depth quantization/filter/device policies are not declared identical merely because standard wire semantics match.',
)

OFFICIAL_LAUNCH_SOURCE = b'''# Public launch code; configuration remains in explicit private files.
import json
from pathlib import Path
from launch import LaunchDescription, LaunchService
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
capture = json.loads(Path('/config/capture.json').read_text())
params = json.loads(Path('/config/official_params.json').read_text())
namespace = capture['ros']['namespace']
node = ComposableNode(package='orbbec_camera', plugin='orbbec_camera::OBCameraNodeDriver',
                      name=params['camera_name'], namespace=namespace, parameters=[params])
container = ComposableNodeContainer(name='gdp_official_benchmark_container', namespace=namespace,
                                   package='rclcpp_components', executable='component_container',
                                   composable_node_descriptions=[node], output='log')
service = LaunchService()
service.include_launch_description(LaunchDescription([container]))
raise SystemExit(service.run())
'''


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def command(args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, 'COMMAND_FAILED')
    return result.stdout


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def benchmark_spec(source, seconds):
    value = json.loads(source.read_bytes())
    for stream in value['streams'].values():
        stream['min_frames'] = math.ceil(seconds * 25)
        stream['min_fps'] = 25
    value['pair']['min_pairs'] = math.ceil(seconds * 25)
    module(ROOT/'runtime/rgbd_acceptance.py', 'bench_observer').validate_spec(value)
    return value


def observer_source():
    source = (ROOT/'runtime/rgbd_acceptance.py').read_text()
    needle = '        start, next_qos, tf_created = time.monotonic(), 0., False\n'
    require(source.count(needle) == 1, 'OBSERVER_INSTRUMENTATION_DRIFT')
    gate = '''        Path('/bench/ready').write_text('ready')
        gate_deadline = time.monotonic() + 30
        while not Path('/bench/go').exists():
            require(time.monotonic() < gate_deadline, 'BENCH_GATE_TIMEOUT')
            time.sleep(.005)
'''
    return source.replace(needle, gate + needle).encode()


def validate_official_params(config, params):
    """Reject mismatches; never rewrite the caller's official parameters."""
    require(isinstance(params, dict), 'OFFICIAL_PARAMS_OBJECT_REQUIRED')
    require(isinstance(config.get('serial'), str) and bool(config['serial']) and
            params.get('serial_number') == config['serial'], 'OFFICIAL_SERIAL_MISMATCH')
    suffix = '_color_optical_frame'
    color_frame, depth_frame = config['color']['frame_id'], config['depth']['frame_id']
    require(color_frame.endswith(suffix), 'OFFICIAL_FRAME_PREFIX_MISMATCH')
    prefix = color_frame[:-len(suffix)]
    require(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', prefix) and
            depth_frame == prefix+'_depth_optical_frame' and params.get('camera_name') == prefix,
            'OFFICIAL_FRAME_PREFIX_MISMATCH')
    for role in ('color', 'depth'):
        require(params.get('enable_'+role) is True, 'OFFICIAL_STREAM_DISABLED')
        for key in ('width', 'height', 'fps'):
            value = params.get(role+'_'+key)
            require(type(value) is int and value == config[role][key], 'OFFICIAL_PROFILE_MISMATCH')
        for key in (role+'_qos', role+'_camera_info_qos'):
            require(params.get(key) == 'DEFAULT', 'OFFICIAL_QOS_POLICY_MISMATCH')
        if role+'_optical_frame_id' in params:
            require(params[role+'_optical_frame_id'] == config[role]['frame_id'], 'OFFICIAL_FRAME_PREFIX_MISMATCH')
    require(params.get('color_format') == 'YUYV' and params.get('depth_format') == 'Y16', 'OFFICIAL_FORMAT_MISMATCH')
    require(params.get('time_domain') == 'system' and params.get('enable_frame_sync') is True and
            params.get('frame_aggregate_mode') == 'ANY', 'OFFICIAL_SYNC_OR_TIME_MISMATCH')
    require(params.get('publish_tf') is True and params.get('device_num') == 1, 'OFFICIAL_DEVICE_OR_TF_POLICY')
    require(all(params.get(key) is False for key in OFFICIAL_DISABLED_FLAGS), 'OFFICIAL_RAW_FILTER_POLICY_REQUIRED')
    for key in ('namespace', 'camera_namespace'):
        if key in params:
            require(params[key] == config['ros']['namespace'], 'OFFICIAL_NAMESPACE_MISMATCH')
    if 'frame_prefix' in params:
        require(params['frame_prefix'] == prefix, 'OFFICIAL_FRAME_PREFIX_MISMATCH')
    for key in ('usb_port', 'load_config_json_file_path', 'config_file_path',
                'depth_filter_config', 'color_filter_config', 'ir_filter_config'):
        require(params.get(key) in (None, ''), 'OFFICIAL_EXTRA_CONFIGURATION_REFUSED')
    for key in ('enable_color_undistortion', 'enable_depth_undistortion', 'enable_enhanced_depth',
                'color_flip', 'depth_flip', 'color_rotation', 'depth_rotation', 'enable_d2c_viewer'):
        require(params.get(key) in (None, False, 0), 'OFFICIAL_TRANSFORM_POLICY_MISMATCH')


def public_profile(config):
    """Only the common standard-wire contract, not internal implementation identity."""
    return {
        'color': {k: config['color'][k] for k in ('width', 'height', 'fps', 'frame_id')},
        'depth': {k: config['depth'][k] for k in ('width', 'height', 'fps', 'frame_id')},
        'color_encoding': 'yuyv', 'depth_encoding': '16UC1', 'depth_unit': 'mm',
        'depth_alignment': 'raw', 'time_source': 'sdk-system-unix',
        'namespace': config['ros']['namespace'], 'configured_software_filters': 'none',
    }


def actual_official_sdk_hashes(producer):
    # Inspect the actual component process mappings after workspace setup, whose
    # LD_LIBRARY_PATH may differ from the configured Rust SDK path.
    script = '''import hashlib,json
from pathlib import Path
paths=set()
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit(): continue
 try:
  for line in (proc/'maps').read_text().splitlines():
   if 'libOrbbecSDK' in line:
    paths.add(line.split(maxsplit=5)[5])
 except (OSError,IndexError): pass
print(json.dumps({'sdk_loaded_hashes':sorted({hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths})}))
'''
    return json.loads(command(['docker', 'exec', producer, 'python3', '-c', script]))['sdk_loaded_hashes']


def common(image, name, uid, domain, stage):
    return ['docker', 'run', '-d', '--init', '--name', name, '--user', uid,
            '--network', 'host', '--ipc', 'private', '--read-only',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '-e', 'ROS_DOMAIN_ID='+str(domain),
            '-e', 'ROS_LOCALHOST_ONLY=0', '-e', 'RMW_IMPLEMENTATION=rmw_fastrtps_cpp',
            '-e', 'FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml',
            '-e', 'ROS_LOG_DIR=/tmp/ros-logs', '-v', str(stage/'config')+':/config:ro']


def wait_file(path, deadline, name):
    while time.monotonic() < deadline:
        if path.exists():
            return
        state = json.loads(command(['docker', 'inspect', name]))[0]['State']
        require(state['Running'], 'CONSUMER_EXITED_BEFORE_WINDOW')
        time.sleep(.05)
    raise ValueError('CONSUMER_READY_TIMEOUT')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--cpp-binary', type=Path)
    parser.add_argument('--official-params', type=Path)
    for key in ('capture-config', 'observer-spec', 'output-dir'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=40)
    parser.add_argument('--trials', type=int, default=4)
    parser.add_argument('--device', required=True)
    parser.add_argument('--domain', type=int, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--devices-released', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    names = []
    log_locations = {}
    owns_output = False
    report = {'version': 1, 'kind': 'same-sdk-ROS-service-benchmark', 'ok': False,
              'hardware_executed': False, 'pure_language_attribution': False,
              'official_driver_compared': False}
    try:
        official_mode = args.official_params is not None
        inputs = [args.capture_config, args.observer_spec, args.output_dir]
        if official_mode:
            inputs.append(args.official_params)
        else:
            require(args.cpp_binary is not None, 'CPP_BINARY_REQUIRED')
            inputs.append(args.cpp_binary)
        require(all(p.is_absolute() for p in inputs), 'ABSOLUTE_PATH_REQUIRED')
        require(not args.output_dir.exists(), 'OUTPUT_EXISTS')
        require(math.isfinite(args.seconds) and 10 <= args.seconds <= 600 and args.trials == 4, 'WINDOW_OR_ORDER_INVALID')
        require(0 <= args.domain <= 232 and re.fullmatch(r'/dev/bus/usb/[0-9]{3}/[0-9]{3}', args.device), 'DEVICE_OR_DOMAIN_INVALID')
        require(args.image and not args.image.startswith('-'), 'IMAGE_INVALID')
        config_bytes = args.capture_config.read_bytes()
        config = json.loads(config_bytes)
        official_bytes = args.official_params.read_bytes() if official_mode else None
        if official_mode:
            validate_official_params(config, json.loads(official_bytes))
        runtime_bytes = args.capture_config.with_name('runtime.json').read_bytes()
        runtime = json.loads(runtime_bytes)
        spec = benchmark_spec(args.observer_spec, args.seconds)
        require(config['pairing'] == {'clock': 'device', 'max_delta_us': 20000}, 'REFERENCE_PAIR_POLICY_UNSUPPORTED')
        require(config['ros']['stamp_source'] == 'sdk-system-unix' and config['depth']['unit_policy'] in ('normalize-mm', 'require-mm'), 'STAMP_OR_UNIT_POLICY_INVALID')
        for role in ('color', 'depth'):
            require(spec['streams'][role]['width'] == 640 and spec['streams'][role]['height'] == 480, 'SPEC_GEOMETRY_DIFFERS')
            require(all(config[role][k] == v for k, v in [('width', 640), ('height', 480), ('fps', 30)]), 'REFERENCE_PROFILE_UNSUPPORTED')
            require(spec['streams'][role]['frame_id'] == config[role]['frame_id'], 'SPEC_BINDING_DIFFERS')
            require(spec['streams'][role]['image_topic'] == config['ros']['namespace'].rstrip('/')+'/'+role+'/image_raw', 'SPEC_BINDING_DIFFERS')
        source = observer_source()
        plan = {'order': ['official', 'rust', 'rust', 'official'] if official_mode else ['cpp', 'rust', 'rust', 'cpp'], 'seconds': args.seconds,
                'producer_stabilization_seconds': 8, 'consumer_discovery_seconds': 8,
                'wire_only': True, 'min_fps': 25, 'min_frames': math.ceil(args.seconds*25),
                'consumer_source_sha256': digest(source), 'spec_sha256': digest(canonical(spec)),
                'capture_config_sha256': digest(config_bytes), 'runtime_manifest_sha256': digest(runtime_bytes),
                'cpp_binary_sha256': None if official_mode else digest(args.cpp_binary.read_bytes()),
                'reference_source_sha256': None if official_mode else digest((ROOT/'tools/validation/sdk_ros_reference.cpp').read_bytes()),
                'official_params_sha256': digest(official_bytes) if official_mode else None,
                'official_launch_source_sha256': digest(OFFICIAL_LAUNCH_SOURCE) if official_mode else None,
                'supervisor_overhead_included_for_rust': True,
                'sync': 'official-explicit-enable/rust-SDK-constructor-enable-attempt' if official_mode else 'SDK-constructor-frame-sync-enable-attempt', 'aggregation': 'SDK-default-ANY',
                'source_filters': 'SDK-default-no-recommended-filter-or-alignment-call',
                'unit_policy': config['depth']['unit_policy']}
        if official_mode:
            plan.update(public_profile_sha256=digest(canonical(public_profile(config))),
                        profile_hash_scope='common-standard-wire-geometry-units-raw-alignment-system-time-and-binding',
                        internal_processing_identical=False,
                        official_initialization_differences=list(OFFICIAL_DIFFERENCES),
                        bridge_used_by_official=False,
                        source_filters='explicit-disabled-official-software-filters; Rust-no-caller-filter-or-alignment-call')
            report['official_driver_selected'] = True
        report['plan'] = plan
        if not args.execute:
            report.update(ok=True, plan_only=True)
            print(json.dumps(report, sort_keys=True)); return 0
        require(args.devices_released and os.geteuid() == 0 and Path(args.device).exists(), 'EXPLICIT_RELEASE_AND_ROOT_REQUIRED')
        # Fail closed if any running container has this device, or a validation
        # service/consumer remains. Never stop another workload to free it.
        ids = command(['docker', 'ps', '-q']).split()
        if ids:
            for running in json.loads(command(['docker', 'inspect', *ids])):
                require(not running['Name'].lstrip('/').startswith(('gdp-service-', 'gdp-consumer-')), 'EXISTING_TEST_WORKLOAD')
                require(all(d.get('PathOnHost') != args.device for d in running['HostConfig'].get('Devices') or []), 'DEVICE_IN_USE')
        image = json.loads(command(['docker', 'image', 'inspect', args.image]))[0]
        image_id = image['Id']
        user = image['Config'].get('User') or '0:0'
        require(re.fullmatch(r'[0-9]+(?::[0-9]+)?', user), 'NUMERIC_IMAGE_USER_REQUIRED')
        uid, _, gid = user.partition(':'); uid = int(uid); gid = int(gid or uid)
        args.output_dir.mkdir(mode=0o700)
        owns_output = True
        os.chown(args.output_dir, uid, gid)
        monitor = module(ROOT/'scripts/validation/resource_monitor.py', 'bench_resource')
        manifests, results = [], []
        for index, implementation in enumerate(plan['order']):
            stage = args.output_dir/('trial-'+str(index+1))
            (stage/'config').mkdir(parents=True, mode=0o700)
            (stage/'capture').mkdir(mode=0o700)
            staged = [(stage/'config/capture.json', config_bytes), (stage/'config/runtime.json', runtime_bytes),
                      (stage/'observer.py', source),
                      (stage/'qos_contract.py', (ROOT/'runtime/qos_contract.py').read_bytes()),
                      (stage/'spec.json', canonical(spec))]
            if official_mode:
                staged.extend([(stage/'config/official_params.json', official_bytes),
                               (stage/'config/official_launch.py', OFFICIAL_LAUNCH_SOURCE)])
            else:
                staged.append((stage/'reference', args.cpp_binary.read_bytes()))
            for path, data in staged:
                path.write_bytes(data); path.chmod(0o700 if path.name == 'reference' else 0o600)
            for path in [stage, *stage.rglob('*')]: os.chown(path, uid, gid)
            prefix = 'gdp-bench-'+uuid.uuid4().hex[:12]
            producer, consumer = prefix+'-p', prefix+'-c'
            names += [producer, consumer]
            log_locations[producer] = stage/'producer-private.log'
            log_locations[consumer] = stage/'consumer-private.log'
            base = common(image_id, producer, user, args.domain, stage)
            # No-device runtime verification uses the actual same immutable image.
            validate = base.copy(); validate[2] = '--rm'; validate[validate.index('--name')+1] += '-validate'
            names.append(prefix+'-p-validate')
            command(validate + ['--entrypoint', '/app/entrypoint.sh', image_id, 'python3', '/app/validate_runtime.py'], 30)
            names.append(prefix+'-hashes')
            measured = command(['docker', 'run', '--rm', '--name', prefix+'-hashes', '--network', 'none', '--read-only', '--entrypoint', 'python3', image_id, '-c',
                "import hashlib,json,platform;from pathlib import Path;paths="+repr([config['sdk_library'], config['ros']['bridge'], '/app/fastdds.xml'])+";print(json.dumps({'hashes':[hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths],'machine':platform.machine(),'kernel':platform.release()}))"])
            observed = json.loads(measured)
            require(observed['hashes'][:2] == [config['runtime_contract']['sdk_sha256'], config['runtime_contract']['bridge_sha256']], 'ACTUAL_LIBRARY_HASH_MISMATCH')
            env = ['-e', 'LD_LIBRARY_PATH='+str(Path(config['sdk_library']).parent)+':/opt/gdp-ros/lib:/opt/ros/humble/lib']
            producer_args = base + ['--device', args.device, '-v', str(stage/'capture')+':/tmp/capture:rw']
            if not official_mode:
                producer_args += ['-v', str(stage/'reference')+':/reference:ro']
            producer_args += env
            if implementation == 'rust':
                producer_args += ['--entrypoint', '/app/entrypoint.sh', image_id, '/usr/local/bin/capture-service', '--config', '/config/capture.json']
            elif implementation == 'official':
                producer_args += ['--entrypoint', '/app/entrypoint.sh', image_id, 'bash', '-lc',
                                  'source /ws/install/setup.bash; exec python3 /config/official_launch.py']
            else:
                # Verify which SDK the reference executable will actually link.
                names.append(prefix+'-ldd')
                linked = command(['docker', 'run', '--rm', '--name', prefix+'-ldd', '--network', 'none', '-v', str(stage/'reference')+':/reference:ro']+env+['--entrypoint', 'ldd', image_id, '/reference'])
                require(any('OrbbecSDK' in line and config['sdk_library'] in line for line in linked.splitlines()), 'REFERENCE_SDK_LINK_DIFFERS')
                producer_args += ['--entrypoint', '/app/entrypoint.sh', image_id, '/reference', '--config', '/config/capture.json', '--seconds', str(args.seconds+60)]
            command(producer_args)
            report['hardware_executed'] = True
            time.sleep(8)
            require(json.loads(command(['docker', 'inspect', producer]))[0]['State']['Running'], 'PRODUCER_START_FAILED')
            if implementation == 'official':
                require(actual_official_sdk_hashes(producer) == [config['runtime_contract']['sdk_sha256']],
                        'OFFICIAL_ACTUAL_SDK_HASH_MISMATCH')
            consumer_args = common(image_id, consumer, user, args.domain, stage)+['-v', str(stage)+':/bench:rw', '--entrypoint', '/app/entrypoint.sh', image_id,
                'python3', '/bench/observer.py', '--spec', '/bench/spec.json', '--wire-only', '--seconds', str(args.seconds), '--discovery-seconds', '8', '--summary', '/bench/observer-summary.json']
            command(consumer_args)
            wait_file(stage/'ready', time.monotonic()+30, consumer)
            groups = [monitor.locate(n) for n in (producer, consumer)]
            start = time.monotonic()
            samples = [{'groups': [monitor.read_group(g) for g in groups]}]
            (stage/'go').write_text('go'); os.chown(stage/'go', uid, gid)
            gate_offset = time.monotonic()-start
            while time.monotonic()-start < args.seconds:
                time.sleep(min(.5, max(0, args.seconds-(time.monotonic()-start))))
                samples.append({'groups': [monitor.read_group(g) for g in groups]})
            elapsed = time.monotonic()-start
            resource = monitor.summarize(samples, elapsed, 2)
            command(['docker', 'wait', consumer], 15)
            observer = json.loads((stage/'observer-summary.json').read_bytes())
            require(observer.get('ok') is True and observer.get('mode') == 'standard-wire' and resource['ok'], 'MEASUREMENT_NOT_ACCEPTED')
            manifest = {'sdk_sha256': observed['hashes'][0], 'bridge_sha256': observed['hashes'][1],
                'profile_sha256': digest(canonical(public_profile(config))) if official_mode else digest(canonical({'color': config['color'], 'depth': config['depth'], 'pairing': config['pairing'], 'warmup': config['startup_discovery_seconds'], 'sync': plan['sync'], 'aggregation': plan['aggregation'], 'filters': plan['source_filters']})),
                'consumer_workload_sha256': digest(source+(ROOT/'runtime/qos_contract.py').read_bytes()+canonical(spec)), 'platform_sha256': digest(canonical({'image_id': image_id, 'platform': observed, 'host': platform.machine()})),
                'trial_id': str(index+1), 'implementation': implementation, 'window_gate_offset_seconds': gate_offset,
                'window_gate_poll_bound_seconds': .005, 'image_id': image_id}
            if official_mode:
                manifest.update(profile_hash_scope=plan['profile_hash_scope'], internal_processing_identical=False,
                                official_params_sha256=digest(official_bytes) if implementation == 'official' else None,
                                official_actual_sdk_hash_verified=implementation == 'official')
            (stage/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
            (stage/'resources.json').write_text(json.dumps(resource, indent=2)+'\n')
            manifests.append(manifest)
            results.append({'trial': index+1, 'implementation': implementation, 'resources': resource,
                            'streams': {r: {k: observer['streams'][r][k] for k in ('images', 'matched_frames', 'active_fps', 'age_ms')} for r in ('color', 'depth')}})
            for name in (producer, consumer):
                log_locations[name].write_text(command(['docker', 'logs', name], 15))
            command(['docker', 'rm', '-f', producer, consumer]); names = []
            time.sleep(3)
        hashes = ('sdk_sha256', 'bridge_sha256', 'profile_sha256', 'consumer_workload_sha256', 'platform_sha256')
        require(all(all(m[k] == manifests[0][k] for k in hashes) for m in manifests), 'RUNS_NOT_COMPARABLE')
        report.update(ok=True, hardware_executed=True, official_driver_compared=official_mode, trials=results, comparable_hashes_verified=True,
                      scope=('Official ROS driver versus supervised Rust service; common standard wire, unequal internal/device initialization policies; not pure-language attribution'
                             if official_mode else 'SDK reference versus supervised Rust service; descriptive alternating trials, not pure-language attribution'))
    except Exception as exc:
        allowed = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_]+', str(exc)) else 'BENCHMARK_FAILED'
        report['errors'] = [allowed]
    finally:
        for name in names:
            if name in log_locations:
                try: log_locations[name].write_text(command(['docker', 'logs', name], 15))
                except Exception: pass
            try: command(['docker', 'rm', '-f', name], 15)
            except Exception: pass
    if owns_output:
        (args.output_dir/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, sort_keys=True)); return 0 if report['ok'] else 1


if __name__ == '__main__': raise SystemExit(main())
