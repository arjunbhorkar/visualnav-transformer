import matplotlib.pyplot as plt
import os
from typing import Tuple, Sequence, Dict, Union, Optional, Callable
import numpy as np
import torch
import torch.nn as nn
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
from omnimimic.policies import GNMPolicy

import matplotlib.pyplot as plt
import yaml

# ROS
import rospy
from sensor_msgs.msg import Image
from std_msgs.msg import Bool, Float32MultiArray
from nav_msgs.msg import Odometry
from utils import msg_to_pil, to_numpy, transform_images, load_model

from vint_train.training.train_utils import get_action
import torch
from PIL import Image as PILImage
import numpy as np
import argparse
import yaml
import time

# UTILS
from topic_names import (IMAGE_TOPIC,
                        WAYPOINT_TOPIC,
                        SAMPLED_ACTIONS_TOPIC)


# CONSTANTS
TOPOMAP_IMAGES_DIR = "../topomaps/images"
MODEL_WEIGHTS_PATH = "../model_weights"
ROBOT_CONFIG_PATH ="../config/robot.yaml"
MODEL_CONFIG_PATH = "../config/models.yaml"
with open(ROBOT_CONFIG_PATH, "r") as f:
    robot_config = yaml.safe_load(f)
MAX_V = robot_config["max_v"]
MAX_W = robot_config["max_w"]
RATE = robot_config["frame_rate"] 
ODOM_TOPIC = "/odom"
WPS = [[0.0021323022460110414, 2.0840790087830972e-07], [0.06273217891681686, 0.00012578891491873316], [0.3619676229196597, 0.0026890932973081113], [0.7218886355890779, -0.0012339311602330132], [1.0626941054908727, -0.03842536054966981], [1.4505703389969717, -0.08042319991650443], [1.7964071397401422, -0.1652326526676752], [2.202620964279936, -0.3668224030326212], [2.5168797890452836, -0.6630042567214929], [2.732661343986673, -1.1282807701720399], [2.754871828184938, -1.6172187410401107], [2.730043944848548, -2.1302715607601006], [2.7325520218874475, -2.6013393300211365], [2.7423687900616334, -3.1226973504048106], [2.7462606852149394, -3.5147190940836643]]


# GLOBALS
context_queue = []
context_size = None  
subgoal = []
obs_odom = None

# Load the model 
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Using device:", device)


def callback_obs(msg):
    obs_img = msg_to_pil(msg)
    if context_size is not None:
        if len(context_queue) < context_size + 1:
            context_queue.append(obs_img)
        else:
            context_queue.pop(0)
            context_queue.append(obs_img)

def make_policy(policy_class, policy_config):
    if policy_class == 'GNM':
        policy = GNMPolicy(**policy_config)
    return policy

def callback_obs_odom(msg: Odometry):
    global obs_odom
    x = msg.pose.pose.position.x
    y = msg.pose.pose.position.y
    obs_odom = [x, y]

def calc_distances(obs_odom):
    distances = []
    for i in range(len(WPS)):
        distances.append(np.linalg.norm(np.array(obs_odom) - np.array(WPS[i])))
    return np.array(distances)

def update_wps(obs_odom):
    dists = calc_distances(obs_odom)
    closest_wp = np.argmin(dists)
    if dists[closest_wp] < 0.1:
        WPS[closest_wp] = max(WPS*2)

def main(args: argparse.Namespace):
    global context_size
    global obs_odom


    odom_curr_msg = rospy.Subscriber(
        ODOM_TOPIC, Odometry, callback_obs_odom, queue_size=1)

     # load model parameters
    with open(MODEL_CONFIG_PATH, "r") as f:
        model_paths = yaml.safe_load(f)

    model_config_path = model_paths[args.model]["config_path"]
    with open(model_config_path, "r") as f:
        model_params = yaml.safe_load(f)
    policy = make_policy("GNM", model_params)
    

    context_size = model_params["context_size"]

    # load model weights
    ckpth_path = model_paths[args.model]["ckpt_path"]
    if os.path.exists(ckpth_path):
        print(f"Loading model from {ckpth_path}")
    else:
        raise FileNotFoundError(f"Model weights not found at {ckpth_path}")
    with open(ckpth_path, 'rb') as f:
        policy.load_state_dict(torch.load(f))
    policy = policy.to(device)
    policy.eval()

    
     # load topomap
    topomap_filenames = sorted(os.listdir(os.path.join(
        TOPOMAP_IMAGES_DIR, args.dir)), key=lambda x: int(x.split(".")[0]))
    topomap_dir = f"{TOPOMAP_IMAGES_DIR}/{args.dir}"
    num_nodes = len(os.listdir(topomap_dir))
    topomap = []
    for i in range(num_nodes):
        image_path = os.path.join(topomap_dir, topomap_filenames[i])
        topomap.append(PILImage.open(image_path))

    closest_node = 0
    assert -1 <= args.goal_node < len(topomap), "Invalid goal index"
    if args.goal_node == -1:
        goal_node = len(topomap) - 1
    else:
        goal_node = args.goal_node
    reached_goal = False

     # ROS
    rospy.init_node("EXPLORATION", anonymous=False)
    rate = rospy.Rate(RATE)
    image_curr_msg = rospy.Subscriber(
        IMAGE_TOPIC, Image, callback_obs, queue_size=1)
    waypoint_pub = rospy.Publisher(
        WAYPOINT_TOPIC, Float32MultiArray, queue_size=1)  
    sampled_actions_pub = rospy.Publisher(SAMPLED_ACTIONS_TOPIC, Float32MultiArray, queue_size=1)
    goal_pub = rospy.Publisher("/topoplan/reached_goal", Bool, queue_size=1)

    print("Registered with master node. Waiting for image observations...")

    if model_params["model_type"] == "nomad":
        num_diffusion_iters = model_params["num_diffusion_iters"]
        noise_scheduler = DDPMScheduler(
            num_train_timesteps=model_params["num_diffusion_iters"],
            beta_schedule='squaredcos_cap_v2',
            clip_sample=True,
            prediction_type='epsilon'
        )
    # navigation loop
    while not rospy.is_shutdown():
        # EXPLORATION MODE
        print("in")
        chosen_waypoint = np.zeros(4)
        if len(context_queue) > model_params["context_size"]:
            if model_params["model_type"] == "nomad":
                obs_images = transform_images(context_queue, model_params["image_size"], center_crop=False)
                obs_images = torch.split(obs_images, 3, dim=1)
                obs_images = torch.cat(obs_images, dim=1) 
                obs_images = obs_images.to(device)
                mask = torch.zeros(1).long().to(device)  

                start = max(closest_node - args.radius, 0)
                end = min(closest_node + args.radius + 1, goal_node)
                goal_image = [transform_images(g_img, model_params["image_size"], center_crop=False).to(device) for g_img in topomap[start:end + 1]]
                goal_image = torch.concat(goal_image, dim=0)

                obsgoal_cond = model('vision_encoder', obs_img=obs_images.repeat(len(goal_image), 1, 1, 1), goal_img=goal_image, input_goal_mask=mask.repeat(len(goal_image)))
                dists = model("dist_pred_net", obsgoal_cond=obsgoal_cond)
                dists = to_numpy(dists.flatten())
                min_idx = np.argmin(dists)
                closest_node = min_idx + start
                print("closest node:", closest_node)
                sg_idx = min(min_idx + int(dists[min_idx] < args.close_threshold), len(obsgoal_cond) - 1)
                obs_cond = obsgoal_cond[sg_idx].unsqueeze(0)

                # infer action
                with torch.no_grad():
                    # encoder vision features
                    if len(obs_cond.shape) == 2:
                        obs_cond = obs_cond.repeat(args.num_samples, 1)
                    else:
                        obs_cond = obs_cond.repeat(args.num_samples, 1, 1)
                    
                    # initialize action from Gaussian noise
                    noisy_action = torch.randn(
                        (args.num_samples, model_params["len_traj_pred"], 2), device=device)
                    naction = noisy_action

                    # init scheduler
                    noise_scheduler.set_timesteps(num_diffusion_iters)

                    start_time = time.time()
                    for k in noise_scheduler.timesteps[:]:
                        # predict noise
                        noise_pred = model(
                            'noise_pred_net',
                            sample=naction,
                            timestep=k,
                            global_cond=obs_cond
                        )
                        # inverse diffusion step (remove noise)
                        naction = noise_scheduler.step(
                            model_output=noise_pred,
                            timestep=k,
                            sample=naction
                        ).prev_sample
                    print("time elapsed:", time.time() - start_time)

                naction = to_numpy(get_action(naction))
                sampled_actions_msg = Float32MultiArray()
                sampled_actions_msg.data = np.concatenate((np.array([0]), naction.flatten()))
                print("published sampled actions")
                sampled_actions_pub.publish(sampled_actions_msg)
                naction = naction[0] 
                chosen_waypoint = naction[args.waypoint]
            elif (len(context_queue) > model_params["context_size"]):
                print("in1")
                start = max(closest_node - args.radius, 0)
                end = min(closest_node + args.radius + 1, goal_node)
                distances = []
                waypoints = []
                batch_obs_imgs = []
                batch_goal_data = []
                for i, sg_img in enumerate(topomap[start: end + 1]):
                    transf_obs_img = transform_images(context_queue, model_params["image_size"])
                    goal_data = transform_images(sg_img, model_params["image_size"])
                    batch_obs_imgs.append(transf_obs_img)
                    batch_goal_data.append(goal_data)
                    
                # predict distances and waypoints
                batch_obs_imgs = torch.cat(batch_obs_imgs, dim=0).to(device)
                batch_goal_data = torch.cat(batch_goal_data, dim=0).to(device)

                distances, waypoints = policy(batch_obs_imgs, batch_goal_data)
                distances = to_numpy(distances)
                waypoints = to_numpy(waypoints)

                waypoints = np.cumsum(waypoints, axis = 1)
                closest_node = np.argmin(distances)
                print(f'Closest node is: {closest_node}, {args.waypoint}')
                # chose subgoal and output waypoints
                if distances[closest_node] > args.close_threshold:
                    chosen_waypoint = waypoints[closest_node][args.waypoint]
                    sg_img = topomap[start + closest_node]
                else:
                    chosen_waypoint = waypoints[min(
                        closest_node + 1, len(waypoints) - 1)][args.waypoint]
                    sg_img = topomap[start + min(closest_node + 1, len(waypoints) - 1)]     
        # RECOVERY MODE

        chosen_waypoint = np.array([-chosen_waypoint[2], chosen_waypoint[1]])
        if model_params["normalize"]:
            chosen_waypoint[:2] *= (MAX_V / RATE)  
        waypoint_msg = Float32MultiArray()
        waypoint_msg.data = chosen_waypoint
        # print(waypoint_msg)
        waypoint_pub.publish(waypoint_msg)
        reached_goal = closest_node == goal_node
        goal_pub.publish(reached_goal)
        # if reached_goal:
        #     print("Reached goal! Stopping...")
        rate.sleep()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Code to run GNM DIFFUSION EXPLORATION on the locobot")
    parser.add_argument(
        "--model",
        "-m",
        default="nomad",
        type=str,
        help="model name (only nomad is supported) (hint: check ../config/models.yaml) (default: nomad)",
    )
    parser.add_argument(
        "--waypoint",
        "-w",
        default=2, # close waypoints exihibit straight line motion (the middle waypoint is a good default)
        type=int,
        help=f"""index of the waypoint used for navigation (between 0 and 4 or 
        how many waypoints your model predicts) (default: 2)""",
    )
    parser.add_argument(
        "--dir",
        "-d",
        default="topomap",
        type=str,
        help="path to topomap images",
    )
    parser.add_argument(
        "--goal-node",
        "-g",
        default=-1,
        type=int,
        help="""goal node index in the topomap (if -1, then the goal node is 
        the last node in the topomap) (default: -1)""",
    )
    parser.add_argument(
        "--close-threshold",
        "-t",
        default=3,
        type=int,
        help="""temporal distance within the next node in the topomap before 
        localizing to it (default: 3)""",
    )
    parser.add_argument(
        "--radius",
        "-r",
        default=4,
        type=int,
        help="""temporal number of locobal nodes to look at in the topopmap for
        localization (default: 2)""",
    )
    parser.add_argument(
        "--num-samples",
        "-n",
        default=8,
        type=int,
        help=f"Number of actions sampled from the exploration model (default: 8)",
    )
    args = parser.parse_args()
    print(f"Using {device}")
    main(args)


