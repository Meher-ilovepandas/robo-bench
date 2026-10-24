"""
Autonomous VEX AIM game controller — Purple targets edition.
Goal: knock down all 4 purple targets using the ball.

Purple targets are detected via AI Vision color detection (Colordesc).
"""
import sys, time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from vex import Robot, KickType, TurnType, AiVision, Colordesc

SCREEN_CENTER_X = 160   # 320px wide frame
DEAD_ZONE       = 30    # px — acceptable alignment error

# Purple color descriptor — hue ~270°, broad tolerance
# Colordesc(index, r, g, b, hue_angle_tolerance, sat_threshold)
PURPLE = Colordesc(1, 140, 30, 180, 25, 0.10)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def get_objects(robot, descriptor):
    return list(robot.vision.get_data(descriptor))

def get_all_ai(robot):
    return list(robot.vision.get_data(AiVision.ALL_AIOBJS))

def largest(objects):
    if not objects:
        return None
    return max(objects, key=lambda o: o.width * o.height)

def align_to(robot, obj, label):
    offset = obj.centerX - SCREEN_CENTER_X
    if abs(offset) <= DEAD_ZONE:
        log(f"  {label} centred (offset={offset:+d}px)")
        return offset
    angle = abs(offset) * 0.22
    direction = TurnType.RIGHT if offset > 0 else TurnType.LEFT
    log(f"  {label} offset={offset:+d}px → turning {'right' if offset>0 else 'left'} {angle:.1f}°")
    robot.turn_for(direction, angle, 40)
    return offset

def spin_scan(robot, descriptor, label, step_deg=45, max_steps=8):
    log(f"Scanning for {label} ...")
    for i in range(max_steps):
        objs = get_objects(robot, descriptor)
        if objs:
            obj = largest(objs)
            log(f"  Found {label}! size={obj.width}x{obj.height} centerX={obj.centerX}")
            return obj
        log(f"  Step {i+1}: turning {step_deg}° right")
        robot.turn_for(TurnType.RIGHT, step_deg, 40)
        time.sleep(0.3)
    log(f"  {label} not found after full scan.")
    return None

def approach(robot, descriptor, label, target_width, max_iters=15):
    log(f"Approaching {label} (want width>{target_width}px) ...")
    for _ in range(max_iters):
        objs = get_objects(robot, descriptor)
        obj = largest(objs)
        if not obj:
            log(f"  Lost {label}, rescanning ...")
            obj = spin_scan(robot, descriptor, label)
            if not obj:
                return False
        log(f"  {label} width={obj.width}px")
        if obj.width >= target_width:
            log(f"  Close enough!")
            return True
        align_to(robot, obj, label)
        step_mm = max(80, min(350, int((target_width / max(obj.width, 1)) * 100)))
        log(f"  Moving forward {step_mm}mm")
        robot.move_for(step_mm, 0, 55)
        time.sleep(0.4)
    return False

# ── Main ──────────────────────────────────────────────────────────────────────
log("Connecting ...")
robot = Robot()
log(f"Connected! Battery: {robot.get_battery_capacity()}%")

# Enable color detection with the purple descriptor
robot.vision.color_detection(True)
robot.vision.color_description(PURPLE)
log("Color detection enabled for purple targets.")

# Quick debug: what can we see right now?
time.sleep(0.5)
purple_now = get_objects(robot, PURPLE)
ai_now = get_all_ai(robot)
log(f"Initial scan — purple objects: {len(purple_now)}, AI objects: {[(o.classname, o.width) for o in ai_now]}")

# ── Phase 1: Get the ball ─────────────────────────────────────────────────────
robot.vision.model_detection(True)
has_ball = robot.has_sports_ball()
log(f"Has ball: {has_ball}")

if not has_ball:
    log("=== PHASE 1: Find ball ===")
    ball = spin_scan(robot, AiVision.ALL_AIOBJS, "SportsBall")
    # filter to just ball
    for _ in range(8):
        ai_objs = get_all_ai(robot)
        balls = [o for o in ai_objs if o.classname == "SportsBall"]
        if balls:
            ball = largest(balls)
            log(f"SportsBall found: size={ball.width}x{ball.height}")
            align_to(robot, ball, "SportsBall")
            close = approach(robot, AiVision.ALL_AIOBJS, "SportsBall", target_width=100)
            if close:
                log("Scooping ball ...")
                robot.move_for(150, 0, 40)
                time.sleep(0.5)
            break
        log(f"  No ball yet, turning 45° ...")
        robot.turn_for(TurnType.RIGHT, 45, 40)
        time.sleep(0.3)

has_ball = robot.has_sports_ball()
log(f"Has ball after Phase 1: {has_ball}")

# ── Phase 2: Knock down purple targets ───────────────────────────────────────
log("=== PHASE 2: Knock purple targets ===")
TARGET_COUNT = 4
targets_knocked = 0

for target_num in range(1, TARGET_COUNT + 1):
    log(f"\n--- Target {target_num}/{TARGET_COUNT} ---")

    target = spin_scan(robot, PURPLE, "PurpleTarget")
    if not target:
        log("No purple targets visible — done or all knocked!")
        break

    # Align
    for _ in range(3):
        offset = align_to(robot, target, "PurpleTarget")
        if abs(offset) <= DEAD_ZONE:
            break
        objs = get_objects(robot, PURPLE)
        if objs:
            target = largest(objs)

    if has_ball:
        close = approach(robot, PURPLE, "PurpleTarget", target_width=100)
        if close:
            log("Kicking HARD!")
            robot.kicker.kick(KickType.HARD)
            time.sleep(2.0)
    else:
        close = approach(robot, PURPLE, "PurpleTarget", target_width=160)
        if close:
            log("Ramming target!")
            robot.move_for(500, 0, 80)
            time.sleep(1.5)

    targets_knocked += 1
    log(f"Target {target_num} done! ({targets_knocked}/{TARGET_COUNT})")

    log("Backing off ...")
    robot.move_for(250, 180, 50)
    time.sleep(0.8)

log(f"\n=== DONE — attacked {targets_knocked}/{TARGET_COUNT} targets ===")
