"""Reliable recording contract, independent of ROS imports and device access.

The pinned public driver's getRMWQosProfileFromString accepts DEFAULT, which maps
onto rmw_qos_profile_default (RELIABLE, VOLATILE). RELIABLE is not itself a valid
Orbbec profile name; do not rely on its error-and-fallback behavior.
"""

# Match the bounded static-transform listener history: multiple independent
# /tf_static writers must fit while the executor is busy with image callbacks.
STATIC_TF_SUBSCRIPTION_DEPTH = 100

QOS_KEYS = tuple(stream + suffix for stream in ('color', 'depth')
                 for suffix in ('_qos', '_camera_info_qos'))


def effective_driver_qos(parameters):
    """Resolve omitted values without accepting unknown or best-effort profiles."""
    result = {}
    for key in QOS_KEYS:
        value = parameters.get(key, 'DEFAULT')
        if not isinstance(value, str) or value.upper() != 'DEFAULT':
            raise ValueError('QOS_CONTRACT: reliable-compatible driver profile required')
        result[key] = 'DEFAULT'
    return result


def validate_offered_qos(endpoint_profiles, required='reliable'):
    """Check discovered image/CameraInfo publishers, never their configured intent.

    Accept rclpy QoSProfile objects or dicts using public DDS policy values.
    Require one unambiguous publisher with RELIABLE reliability and a known
    durability that can satisfy a VOLATILE subscriber. Return sanitized status;
    never echo graph node names, endpoint GIDs or other runtime bindings.
    """
    if required != 'reliable':
        raise ValueError('QOS_CONTRACT: unsupported consumer requirement')
    profiles = list(endpoint_profiles)
    if len(profiles) != 1:
        return {'ready': False, 'code': 'QOS_PUBLISHER_COUNT'}
    profile = profiles[0]
    get = profile.get if isinstance(profile, dict) else lambda k: getattr(profile, k, None)
    # rclpy IntEnums use standard RMW policy values: reliability RELIABLE=1,
    # durability TRANSIENT_LOCAL=1 / VOLATILE=2. UNKNOWN/default never passes.
    reliability, durability = get('reliability'), get('durability')
    if reliability not in (1, 'RELIABLE', 'reliable') or type(reliability) is bool:
        return {'ready': False, 'code': 'QOS_RELIABILITY'}
    if durability not in (1, 2, 'VOLATILE', 'volatile', 'TRANSIENT_LOCAL', 'transient_local') or type(durability) is bool:
        return {'ready': False, 'code': 'QOS_DURABILITY'}
    return {'ready': True, 'code': 'QOS_COMPATIBLE'}
