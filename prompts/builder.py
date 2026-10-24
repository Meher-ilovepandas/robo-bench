"""
Builds the conversation messages sent to each model.

Prompt 1 (attempt == 1):
    system: game objective (from file)
    user:   starting image + VEX API reference + "Generate code..."

Prompt 2/3 (attempt > 1):
    [full prior conversation preserved externally]
    user (new message):
        all previous codes labeled "Attempt X:"
        all 60 game photos from the latest run
        console log from the latest run
        telemetry from the latest run
        "this is the result of your three trials"
        "Generate your next code"
"""
from __future__ import annotations

from pathlib import Path

from models.base import Message, TextPart, ImagePart


class PromptBuilder:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._system_prompt = Path(cfg["paths"]["system_prompt_file"]).read_text()
        vex_api_file = Path(cfg["paths"]["vex_api_file"])
        self._vex_api = vex_api_file.read_text() if vex_api_file.exists() else (
            "[VEX API reference not found. Run download_api.py first.]"
        )

    # ------------------------------------------------------------------ #
    #  First prompt                                                        #
    # ------------------------------------------------------------------ #

    def build_first_prompt(
        self,
        starting_image: tuple[str, str] | None,   # (base64, media_type) or None
    ) -> list[Message]:
        """
        Returns [system_message, user_message] for the first code generation.
        starting_image is the overhead snapshot taken before any run starts.
        """
        system_text = (
            "You are a Python code generator for a VEX AIM educational robotics competition. "
            "This is a safe, supervised robot sports game where robots maneuver a ball to knock "
            "over targets on a small 16\"×16\" game field. Generate Python code only.\n\n"
            + self._system_prompt
        )
        system_msg = Message(role="system", parts=[TextPart(system_text)])

        user_parts: list[TextPart | ImagePart] = []

        if starting_image:
            user_parts.append(TextPart("Here is the starting state of the game board:"))
            user_parts.append(ImagePart(data=starting_image[0], media_type=starting_image[1]))

        user_parts.append(TextPart(
            "EXECUTION MODEL:\n"
            "Your code runs on the HOST computer (not on the robot). "
            "It communicates with the VEX AIM robot over Wi-Fi using the official "
            "VEX AIM WebSocket library. Commands are sent to the robot in real time "
            "as each function is called. Movement commands block until complete by default.\n\n"
            "IMPORTS (use ONLY these):\n"
            "  from vex import Robot, Inertial, Screen, Kicker, Sound, Led, AiVision, Timer, Thread\n"
            "  from vex import (TurnType, RotationUnits, DistanceUnits, TimeUnits,\n"
            "                   SoundType, FontType, AxisType,\n"
            "                   EmojiType, LightType, ColorRGB,\n"
            "                   PERCENT, MMPS, DPS, MSEC, SECONDS)\n\n"
            "INITIALIZATION (always start with this):\n"
            "  robot = Robot()   # connects to robot, starts program\n\n"
            "KEY API:\n\n"
            "# Movement (distances in mm, angles in degrees, velocity 0-100 PERCENT or mm/s)\n"
            "robot.move_for(distance_mm, angle_deg, velocity=50)  # blocking by default; angle 0=forward, 90=right\n"
            "robot.move_at(angle_deg, velocity=50)                # non-blocking continuous move\n"
            "robot.turn_for(TurnType.RIGHT, degrees, velocity=50) # blocking turn\n"
            "robot.turn_to(heading_deg, velocity=50)              # turn to absolute heading\n"
            "robot.turn(TurnType.LEFT, velocity=50)               # non-blocking continuous turn\n"
            "robot.stop_all_movement()\n"
            "robot.set_move_velocity(velocity, VelocityUnits.PERCENT)\n"
            "robot.set_turn_velocity(velocity, VelocityUnits.PERCENT)\n"
            "robot.spin_wheels(vel1, vel2, vel3)                  # direct wheel control\n\n"
            "# State\n"
            "robot.is_move_active()   # True if move_at/move_for running\n"
            "robot.is_turn_active()   # True if turn running\n"
            "robot.is_stopped()       # True if no wheels moving\n"
            "robot.get_x_position()   # odometry x (mm)\n"
            "robot.get_y_position()   # odometry y (mm)\n"
            "robot.get_battery_capacity()   # battery %\n"
            "robot.has_blue_barrel()  / robot.has_orange_barrel()  / robot.has_sports_ball()\n\n"
            "# Kicker\n"
            "robot.kicker.kick(kick_type)  # kick_type: 'kick_low', 'kick_mid', 'kick_high'\n"
            "robot.kicker.place()          # gently place object\n\n"
            "# Inertial sensor\n"
            "robot.inertial.get_heading()      # 0-359.99 degrees\n"
            "robot.inertial.get_rotation()     # cumulative rotation\n"
            "robot.inertial.get_pitch()        # -90 to 90\n"
            "robot.inertial.get_roll()         # -180 to 180\n"
            "robot.inertial.get_acceleration(AxisType.XAXIS)   # m/s^2\n"
            "robot.inertial.get_turn_rate(AxisType.ZAXIS)      # deg/s\n"
            "robot.inertial.reset_heading()    # set heading to 0\n"
            "robot.inertial.set_heading(deg)   # set heading to value\n"
            "robot.inertial.calibrate()        # calibrate IMU\n\n"
            "# Screen\n"
            "robot.screen.print('text')        # print at cursor\n"
            "robot.screen.print_at('text', x=10, y=20)\n"
            "robot.screen.set_cursor(row, col)\n"
            "robot.screen.next_row()\n"
            "robot.screen.clear_screen()\n"
            "robot.screen.draw_rectangle(x, y, width, height)\n"
            "robot.screen.draw_circle(x, y, radius)\n\n"
            "# Sound\n"
            "robot.sound.play(SoundType.COMPLETE)  # built-in sounds\n"
            "robot.sound.stop()\n\n"
            "# LED\n"
            "robot.led.on(LightType.LED1, ColorRGB(255, 0, 0))\n"
            "robot.led.off(LightType.ALL_LEDS)\n\n"
            "# AI Vision\n"
            "robot.aivision.model_detection(True)    # enable AI object detection\n"
            "robot.aivision.color_detection(True)    # enable color detection\n"
            "robot.aivision.tag_detection(True)      # enable AprilTag detection\n"
            "robot.aivision.get_data(AiObjdesc(0))   # get objects of class 0 (SportsBall)\n"
            "# AiObjdesc indices: 0=SportsBall, 1=BlueBarrel, 2=OrangeBarrel, 3=Robot\n"
            "count = robot.aivision.object_count()\n"
            "obj = robot.aivision.largest_object()   # VisionObject\n"
            "# obj.originx, obj.originy, obj.width, obj.height, obj.score\n\n"
            "# Timer\n"
            "t = Timer()\n"
            "t.time(TimeUnits.MSEC)   # elapsed ms\n"
            "t.reset()\n"
            "t.event(callback, delay_ms)   # fire callback after delay\n\n"
            "# Thread\n"
            "def my_func(): pass\n"
            "Thread(my_func)   # run concurrently\n\n"
            "# Timing\n"
            "from vex import wait, MSEC, SECONDS\n"
            "wait(500, MSEC)     # wait 500 ms\n"
            "wait(1, SECONDS)\n\n"
            "USE print() for any values you want logged.\n\n"
            "Generate Python code for the VEX AIM robot to accomplish the objective. "
            "Return ONLY valid Python source code — no markdown fences, no explanations."
        ))

        user_msg = Message(role="user", parts=user_parts)
        return [system_msg, user_msg]

    # ------------------------------------------------------------------ #
    #  Subsequent prompts (attempt 2 and 3)                               #
    # ------------------------------------------------------------------ #

    def build_feedback_message(
        self,
        all_codes: list[tuple[int, str]],           # [(attempt_num, code), ...]
        run_photos: list[tuple[str, str]],           # [(base64, media_type), ...]  all 60
        console_log: str,
        telemetry_text: str,
    ) -> Message:
        """
        Returns the user Message for attempt 2 or 3.
        The caller must append this to the existing conversation history
        (which already contains the system message, prior user prompts, and
        model responses).
        """
        parts: list[TextPart | ImagePart] = []

        # Previous generated codes
        for attempt_num, code in all_codes:
            parts.append(TextPart(
                f"Attempt {attempt_num}:\n```python\n{code}\n```"
            ))

        # Game photos (all frames in chronological order)
        if run_photos:
            parts.append(TextPart(
                f"Game photos from your latest run ({len(run_photos)} frames, chronological order):"
            ))
            for b64, mt in run_photos:
                parts.append(ImagePart(data=b64, media_type=mt))

        # Console log
        parts.append(TextPart(
            "Console output from the latest run:\n"
            "```\n" + console_log + "\n```"
        ))

        # Telemetry
        parts.append(TextPart(
            "Robot telemetry from the latest run:\n"
            "```\n" + telemetry_text + "\n```"
        ))

        parts.append(TextPart("this is the result of your three trials"))
        parts.append(TextPart(
            "Generate your next code. "
            "Return ONLY valid Python source code — no markdown fences, no explanations."
        ))

        return Message(role="user", parts=parts)
