"""An OpenAI Gym environment for Super Mario Bros. and Lost Levels."""

from collections import defaultdict
from collections.abc import Callable

import numpy as np
from nes_py import NESEnv  # pyright: ignore[reportMissingImports]

from ._roms import decode_target, rom_path

# create a dictionary mapping value of status register to string names
_STATUS_MAP = defaultdict(lambda: "fireball", {0: "small", 1: "tall"})


# a set of state values indicating that Mario is "busy"
_BUSY_STATES = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x07]


# RAM addresses for enemy types on the screen
_ENEMY_TYPE_ADDRESSES = [0x0016, 0x0017, 0x0018, 0x0019, 0x001A]


# enemies whose context indicate that a stage change will occur (opposed to an
# enemy that implies a stage change won't occur -- i.e., a vine)
# Bowser = 0x2D
# Flagpole = 0x31
_STAGE_OVER_ENEMIES = np.array([0x2D, 0x31])


class SuperMarioBrosEnv(NESEnv):
    """An environment for playing Super Mario Bros with OpenAI Gym."""

    # the legal range of rewards for each step
    reward_range = (-15, 15)

    def __init__(
        self,
        rom_mode: str = "vanilla",
        lost_levels: bool = False,
        target: tuple[int, int] | None = None,
        max_episode_steps: int | None = None,
        truncate_function: Callable | None = None,
    ):
        """Initialize a new Super Mario Bros environment.

        Args:
            rom_mode (str): the ROM mode to use when loading ROMs from disk
            lost_levels (bool): whether to load the ROM with lost levels.
                - False: load original Super Mario Bros.
                - True: load Super Mario Bros. Lost Levels
            target (tuple): a tuple of the (world, stage) to play as a level
            max_episode_steps (int, optional): the maximum number of steps per episode before truncation
            truncate_function (Callable, None): a function to determine if the episode should be truncated it must take the 3 following arguments:
            - self: the environment instance (to possibly access / add instance variables)
            - reward: the reward received from the last step
            - info: the info dictionary returned from the last step

        Returns:
            None

        """
        # decode the ROM path based on mode and lost levels flag
        rom = rom_path(lost_levels, rom_mode)
        # initialize the super object with the ROM path
        super().__init__(
            rom,
            max_episode_steps=max_episode_steps,
            truncate_function=truncate_function,
        )
        # set the target world, stage, and area variables
        self._target_world, self._target_stage, self._target_area = decode_target(
            target, lost_levels
        )
        # setup a variable to keep track of the last frames time
        self._time_last = 0

        # reset the emulator
        self.reset()
        # skip the start screen
        self._skip_start_screen()
        # create a backup state to restore from on subsequent calls to reset
        self._backup()

    @property
    def is_single_stage_env(self):
        """Return True if this environment is a stage environment."""
        return self._target_world is not None and self._target_area is not None

    # MARK: Memory access

    def _read_mem_range(self, address, length):
        """Read a range of bytes where each byte is a 10's place figure.

        Args:
            address (int): the address to read from as a 16 bit integer
            length: the number of sequential bytes to read

        Note:
            this method is specific to Mario where three GUI values are stored
            in independent memory slots to save processing time
            - score has 6 10's places
            - coins has 2 10's places
            - time has 3 10's places

        Returns:
            the integer value of this 10's place representation

        """
        return int("".join(map(str, self.ram[address : address + length])))

    @property
    def _level(self):
        """Return the level of the game."""
        return self.ram[0x075F] * 4 + self.ram[0x075C]

    @property
    def _world(self):
        """Return the current world (1 to 8)."""
        return int(self.ram[0x075F]) + 1

    @property
    def _stage(self):
        """Return the current stage (1 to 4)."""
        return int(self.ram[0x075C]) + 1

    @property
    def _area(self):
        """Return the current area number (1 to 5)."""
        return self.ram[0x0760] + 1

    @property
    def _score(self):
        """Return the current player score (0 to 999990)."""
        # score is represented as a figure with 6 10's places
        return self._read_mem_range(0x07DE, 6)

    @property
    def _time(self):
        """Return the time left (0 to 999)."""
        # time is represented as a figure with 3 10's places
        return self._read_mem_range(0x07F8, 3)

    @property
    def _coins(self):
        """Return the number of coins collected (0 to 99)."""
        # coins are represented as a figure with 2 10's places
        return self._read_mem_range(0x07ED, 2)

    @property
    def _life(self):
        """Return the number of remaining lives."""
        return int(self.ram[0x075A])

    @property
    def _x_position(self):
        """Return the current horizontal position."""
        # add the current page 0x6d to the current x
        return int(self.ram[0x6D]) * 0x100 + int(self.ram[0x86])

    @property
    def _left_x_position(self):
        """Return the number of pixels from the left of the screen."""
        # TODO: resolve RuntimeWarning: overflow encountered in ubyte_scalars
        # subtract the left x position 0x071c from the current x 0x86
        # return (self.ram[0x86] - self.ram[0x071c]) % 256
        return np.uint8(int(self.ram[0x86]) - int(self.ram[0x071C])) % 256

    @property
    def _y_pixel(self):
        """Return the current vertical position."""
        return int(self.ram[0x03B8])

    @property
    def _y_viewport(self):
        """Return the current y viewport.

        Note:
            1 = in visible viewport
            0 = above viewport
            > 1 below viewport (i.e. dead, falling down a hole)
            up to 5 indicates falling into a hole

        """
        return self.ram[0x00B5]

    @property
    def _y_position(self):
        """Return the current vertical position."""
        # check if Mario is above the viewport (the score board area)
        if self._y_viewport < 1:
            # y position overflows so we start from 255 and add the offset
            return 255 + (255 - self._y_pixel)
        # check if Mario is under the viewport (falling into a pit)
        elif self._y_viewport > 1:
            # y position goes into the negatives on 8 bits
            return -self._y_pixel - 1
        # invert the y pixel into the distance from the bottom of the screen
        return 255 - self._y_pixel

    @property
    def _player_status(self):
        """Return the player status as a string."""
        return _STATUS_MAP[self.ram[0x0756]]

    @property
    def _player_state(self):
        """Return the current player state.

        Note:
            0x00 : Leftmost of screen
            0x01 : Climbing vine
            0x02 : Entering reversed-L pipe
            0x03 : Going down a pipe
            0x04 : Auto-walk
            0x05 : Auto-walk
            0x06 : Dead
            0x07 : Entering area
            0x08 : Normal
            0x09 : Cannot move
            0x0B : Dying
            0x0C : Palette cycling, can't move

        """
        return self.ram[0x000E]

    @property
    def _is_dying(self):
        """Return True if Mario is in dying animation, False otherwise."""
        return self._player_state == 0x0B or self._y_viewport > 1

    @property
    def _is_dead(self):
        """Return True if Mario is dead, False otherwise."""
        return self._player_state == 0x06

    @property
    def _is_game_over(self):
        """Return True if the game has ended, False otherwise."""
        # the life counter will get set to 255 (0xff) when there are no lives
        # left. It goes 2, 1, 0 for the 3 lives of the game
        return self._life == 0xFF

    @property
    def _is_busy(self):
        """Return boolean whether Mario is busy with in-game garbage."""
        return self._player_state in _BUSY_STATES

    @property
    def _is_world_over(self):
        """Return a boolean determining if the world is over."""
        # 0x0770 contains GamePlay mode:
        # 0 => Demo
        # 1 => Standard
        # 2 => End of world
        return self.ram[0x0770] == 2

    @property
    def _is_stage_over(self):
        """Return a boolean determining if the level is over."""
        # iterate over the memory addresses that hold enemy types
        for address in _ENEMY_TYPE_ADDRESSES:
            # check if the byte is either Bowser (0x2D) or a flag (0x31)
            # this is to prevent returning true when Mario is using a vine
            # which will set the byte at 0x001D to 3
            if self.ram[address] in _STAGE_OVER_ENEMIES:
                # player float state set to 3 when sliding down flag pole
                return self.ram[0x001D] == 3

        return False

    @property
    def _flag_get(self):
        """Return a boolean determining if the agent reached a flag."""
        return self._is_world_over or self._is_stage_over

    # MARK: RAM Hacks

    def _write_stage(self):
        """Write the stage data to RAM to overwrite loading the next stage."""
        if (
            self._target_world is not None
            and self._target_stage is not None
            and self._target_area is not None
        ):
            self.ram[0x075F] = self._target_world - 1
            self.ram[0x075C] = self._target_stage - 1
            self.ram[0x0760] = self._target_area - 1

    def _runout_prelevel_timer(self):
        """Force the pre-level timer to 0 to skip frames during a death."""
        self.ram[0x07A0] = 0

    def _skip_change_area(self):
        """Skip change area animations by by running down timers."""
        change_area_timer = self.ram[0x06DE]
        if change_area_timer > 1 and change_area_timer < 255:
            self.ram[0x06DE] = 1

    def _skip_occupied_states(self):
        """Skip occupied states by running out a timer and skipping frames."""
        while self._is_busy or self._is_world_over:
            self._runout_prelevel_timer()
            self._frame_advance(0)

    def _skip_start_screen(self):
        """Press and release start to skip the start screen."""
        # press and release the start button
        self._frame_advance(8)
        self._frame_advance(0)
        # Press start until the game starts
        while self._time == 0:
            # press and release the start button
            self._frame_advance(8)
            # if we're in the single stage, environment, write the stage data
            if self.is_single_stage_env:
                self._write_stage()
            self._frame_advance(0)
            # run-out the prelevel timer to skip the animation
            self._runout_prelevel_timer()
        # set the last time to now
        self._time_last = self._time
        # after the start screen idle to skip some extra frames
        while self._time >= self._time_last:
            self._time_last = self._time
            self._frame_advance(8)
            self._frame_advance(0)

    def _skip_end_of_world(self):
        """Skip the cutscene that plays at the end of a world."""
        if self._is_world_over:
            # get the current game time to reference
            time = self._time
            # loop until the time is different
            while self._time == time:
                # frame advance with NOP
                self._frame_advance(0)

    def _kill_mario(self):
        """Skip a death animation by forcing Mario to death."""
        # force Mario's state to dead
        self.ram[0x000E] = 0x06
        # step forward one frame
        self._frame_advance(0)

    # MARK: Reward Function

    @property
    def _x_reward(self):
        """Return the reward based on left right movement between steps."""
        _reward = self._x_position - self.last_frame_x_position
        # TODO: check whether this is still necessary
        # resolve an issue where after death the x position resets. The x delta
        # is typically has at most magnitude of 3, 5 is a safe bound
        if _reward < -5 or _reward > 5:
            return 0

        return _reward

    @property
    def _time_penalty(self):
        """Return the reward for the in-game clock ticking."""
        _reward = self._time - self._time_last
        self._time_last = self._time
        # time can only decrease, a positive reward results from a reset and
        # should default to 0 reward
        if _reward > 0:
            return 0

        return _reward

    @property
    def _death_penalty(self):
        """Return the reward earned by dying."""
        if self._is_dying or self._is_dead:
            return -25

        return 0

    # MARK: nes-py API calls

    def _will_reset(self):
        """Handle and RAM hacking before a reset occurs."""
        self._time_last = 0

    def _did_reset(self):
        """Handle any RAM hacking after a reset occurs."""
        self._time_last = self._time
        self.last_frame_x_position = self._x_position
        self.last_frame_y_position = self._y_position

    def _did_step(self, will_reset):
        """Handle any RAM hacking after a step occurs.

        Args:
            will_reset: whether the state flag is set to true

        Returns:
            None

        """
        # if will_reset flag is set a reset is incoming anyway, ignore any hacking
        if will_reset:
            return
        # if mario is dying, then cut to the chase and kill hi,
        if self._is_dying:
            self._kill_mario()
        # skip world change scenes (must call before other skip methods)
        if not self.is_single_stage_env:
            self._skip_end_of_world()
        # skip area change (i.e. enter pipe, flag get, etc.)
        self._skip_change_area()
        # skip occupied states like the black screen between lives that shows
        # how many lives the player has left
        self._skip_occupied_states()

    def _get_reward(self):
        """Return the reward after a step occurs."""
        return self._x_reward + self._time_penalty + self._death_penalty

    def _get_terminated(self):
        """Return True if the episode is over, False otherwise."""
        if self.is_single_stage_env:
            return self._is_dying or self._is_dead or self._flag_get
        return self._is_game_over

    def _get_info(self):
        """Return the info after a step occurs."""
        return dict(
            coins=self._coins,
            flag_get=self._flag_get,
            life=self._life,
            score=self._score,
            stage=self._stage,
            status=self._player_status,
            time=self._time,
            world=self._world,
            x_pos=self._x_position,
            y_pos=self._y_position,
            x_speed=self._x_position - self.last_frame_x_position,
            y_speed=self._y_position - self.last_frame_y_position,
        )

    def step(self, action):
        """Run one frame of the NES and return the relevant observation data.

        Args:
            action (byte): the bitmap determining which buttons to press

        Returns:
            a tuple of:
            - observation (np.ndarray): next frame as a result of the given action
            - reward (float) : amount of reward returned after given action
            - terminated (boolean): whether the episode has ended naturally or not (e.g., Mario died, Mario completed the stage)
            - truncated (boolean): whether the episode was truncated by either reaching the maximum number of steps or the truncate function returning True
            - info (dict): contains auxiliary diagnostic information
        """
        self.last_frame_x_position = self._x_position
        self.last_frame_y_position = self._y_position
        return super().step(action)


# explicitly define the outward facing API of this module
__all__ = [SuperMarioBrosEnv.__name__]  # pyright: ignore [reportUnsupportedDunderAll]
