"""
Robot Controller — single-action CLI tool.
Usage: python3 rc.py <command> [args...]

Commands:
  status                    — print position, heading, battery, vision
  scan                      — print all visible objects (AI + tags + colors)
  move <mm> [angle] [vel]   — move (angle 0=fwd, 180=back, default vel=55)
  turn <left|right> <deg>   — turn in place
  turn_to <heading>         — turn to absolute heading
  kick <soft|medium|hard>   — fire kicker
  place                     — place (soft kicker)
  stop                      — stop all movement
  has_ball                  — check if robot holds a ball
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from vex import Robot, KickType, TurnType, AiVision, Tagdesc, Colordesc

def make_robot():
    import time
    r = Robot()
    r.vision.model_detection(True)
    r.vision.tag_detection(True)
    time.sleep(0.4)   # let ws_status refresh after connect
    return r

def fmt_obj(o):
    name = getattr(o, 'classname', None) or f"tag#{getattr(o, 'id', '?')}"
    return f"{name} w={o.width} h={o.height} cx={o.centerX} cy={o.centerY} score={getattr(o,'score','-')}"

def cmd_status(robot, args):
    s = robot.status
    rob = s["robot"]
    ai  = s["aivision"]
    print(f"heading={rob['heading']}  pitch={rob['pitch']}  roll={rob['roll']}")
    print(f"pos x={rob['robot_x']} y={rob['robot_y']}")
    print(f"battery={rob['battery']}%")
    print(f"has_ball={robot.has_sports_ball()}  has_blue={robot.has_blue_barrel()}  has_orange={robot.has_orange_barrel()}")
    print(f"ai_objects={ai['objects']['count']}")

def cmd_scan(robot, args):
    # AI model objects (SportsBall, BlueBarrel, OrangeBarrel, Robot)
    ai_objs = list(robot.vision.get_data(AiVision.ALL_AIOBJS))
    print(f"AI objects ({len(ai_objs)}):")
    for o in ai_objs:
        print(f"  {fmt_obj(o)}")

    # AprilTags 1-6
    tag_objs = []
    for tag_id in range(1, 7):
        t = Tagdesc(tag_id)
        found = list(robot.vision.get_data(t))
        for o in found:
            o._tag_id = tag_id
            tag_objs.append(o)
    print(f"Tags ({len(tag_objs)}):")
    for o in tag_objs:
        print(f"  tag#{o._tag_id} w={o.width} h={o.height} cx={o.centerX} cy={o.centerY}")

def cmd_move(robot, args):
    mm  = float(args[0]) if args else 300
    ang = float(args[1]) if len(args) > 1 else 0
    vel = float(args[2]) if len(args) > 2 else 55
    print(f"move_for(dist={mm}mm, angle={ang}°, vel={vel}%)")
    robot.move_for(mm, ang, vel)
    print("done")

def cmd_turn(robot, args):
    direction = TurnType.RIGHT if (args[0].lower() == 'right') else TurnType.LEFT
    angle = float(args[1]) if len(args) > 1 else 45
    vel   = float(args[2]) if len(args) > 2 else 45
    print(f"turn_for({args[0]}, {angle}°, {vel}%)")
    robot.turn_for(direction, angle, vel)
    print("done")

def cmd_turn_to(robot, args):
    heading = float(args[0])
    vel = float(args[1]) if len(args) > 1 else 45
    print(f"turn_to(heading={heading}°, vel={vel}%)")
    robot.turn_to(heading, vel)
    print("done")

def cmd_kick(robot, args):
    strength = (args[0].lower() if args else "hard")
    kt = {"soft": KickType.SOFT, "medium": KickType.MEDIUM, "hard": KickType.HARD}.get(strength, KickType.HARD)
    print(f"kicker.kick({strength})")
    robot.kicker.kick(kt)
    print("done")

def cmd_place(robot, args):
    print("kicker.place()")
    robot.kicker.place()
    print("done")

def cmd_stop(robot, args):
    print("stop_all_movement()")
    robot.stop_all_movement()
    print("done")

def cmd_has_ball(robot, args):
    print(f"has_ball={robot.has_sports_ball()}")

def cmd_scan2(robot, args):
    """Scan twice with a 0.3s gap and show union of results."""
    import time
    objs1 = cmd_scan(robot, args)
    time.sleep(0.3)
    cmd_scan(robot, args)

def cmd_photo(robot, args):
    """Capture overhead field photo from cam0 and save to /tmp/field.jpg"""
    import cv2, time
    cap = cv2.VideoCapture(0)
    for _ in range(20): cap.read()
    ret, frame = cap.read()
    cap.release()
    if ret:
        cv2.imwrite('/tmp/field.jpg', frame)
        print(f"Photo saved: {frame.shape}")
    else:
        print("Camera capture failed")

COMMANDS = {
    "status":  cmd_status,
    "scan":    cmd_scan,
    "scan2":   cmd_scan2,
    "photo":   cmd_photo,
    "move":    cmd_move,
    "turn":    cmd_turn,
    "turn_to": cmd_turn_to,
    "kick":    cmd_kick,
    "place":   cmd_place,
    "stop":    cmd_stop,
    "has_ball":cmd_has_ball,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    cmd  = sys.argv[1]
    args = sys.argv[2:]
    robot = make_robot()
    COMMANDS[cmd](robot, args)
