"""Quick test: connect to VEX AIM robot over WebSocket and run a simple move."""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from vex import Robot, PERCENT, MSEC

print("Connecting to robot at 192.168.5.100 ...")
robot = Robot()
print("Connected!")

print("Status:", robot.status)

print("Driving forward 300mm ...")
robot.move_for(300, 0, 50)   # 300mm forward at 50%

print("Turning 90 degrees right ...")
robot.turn_for(90, 50)       # 90° right at 50%

print("Driving back 300mm ...")
robot.move_for(300, 180, 50) # 300mm backward at 50%

print("Done.")
