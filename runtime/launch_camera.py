"""Keep legacy launch behavior; bind v2 ROS namespace independently from optical frames."""
import os
from validate_runtime import read_parameters, launch_arguments


def explicit_description(params, namespace, logical_id):
    from launch import LaunchDescription
    from launch_ros.actions import ComposableNodeContainer
    from launch_ros.descriptions import ComposableNode
    node = ComposableNode(package='orbbec_camera', plugin='orbbec_camera::OBCameraNodeDriver',
                          name=params['camera_name'], namespace=namespace, parameters=[params],
                          remappings=[('/diagnostics', namespace + '/diagnostics')])
    container = ComposableNodeContainer(name=logical_id + '_container', namespace=namespace,
                                        package='rclcpp_components', executable='component_container',
                                        composable_node_descriptions=[node], output='log')
    return LaunchDescription([container])


def main():
    params = read_parameters()
    if os.environ.get('CAMERA_SCHEMA', '1') == '2':
        from launch import LaunchService
        service = LaunchService()
        service.include_launch_description(explicit_description(params, os.environ['CAMERA_NAMESPACE'],
                                                                os.environ['CAMERA_ROLE']))
        return service.run()
    argv = ['ros2', 'launch', 'orbbec_camera', os.environ['CAMERA_LAUNCH'], *launch_arguments(params)]
    os.execvp(argv[0], argv)


if __name__ == '__main__': raise SystemExit(main())
