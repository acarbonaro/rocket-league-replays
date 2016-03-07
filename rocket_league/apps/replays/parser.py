import json
import pickle
import sys
from pprint import pprint

from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from bitstring import Bits
from pyrope import Replay


class Parser(object):

    actor_metadata = {}
    goal_metadata = {}
    match_metadata = {}
    actors = {}

    def __init__(self, file_path, parse_netstream=False):
        self.replay = Replay(file_path)
        self.replay_id = self.replay.header['Id']

        pickle_filename = 'uploads/pickle_files/{}.pickle'.format(self.replay_id)

        if parse_netstream:
            try:
                self.replay = pickle.loads(default_storage.open(pickle_filename).read())
            except Exception as e:
                print(e)
                self.replay.parse_netstream()
                default_storage.save(pickle_filename, ContentFile(pickle.dumps(self.replay)))

        if not parse_netstream:
            return

        # Extract the goal information.
        if 'Goals' in self.replay.header:
            for goal in self.replay.header['Goals']:
                self._extract_goal_data(goal['frame'])

        self._get_actors()

        for player in self.actors.copy():
            # Get their position data.
            if self.actors[player]['type'] == 'player':
                self.actors[player]['position_data'] = self._get_player_position_data(player)
            elif self.actors[player]['type'] == 'ball':
                if 'ball' not in self.actors:
                    self.actors['ball'] = {
                        'position_data': {}
                    }

                ball_data = self._get_player_position_data(player)

                self.actors['ball']['position_data'] = {
                    **self.actors['ball']['position_data'],
                    **ball_data
                }

                del self.actors[player]

        collated_data = {
            'actors': self.actors,
            'goal_metadata': self.goal_metadata,
            'actor_metadata': self.actor_metadata,
            'match_metadata': self.match_metadata,
        }

        self.json = json.dumps(collated_data, indent=2)

    def _get_match_metadata(self, frame):
        # Search through the frames looking for some game replication info.
        game_info = [
            value for name, value in frame.actors.items()
            if (
                'GameReplicationInfoArchetype' in name and
                'Engine.GameReplicationInfo:ServerName' in value['data']
            )
        ]

        if not game_info:
            return

        game_info = game_info[0]['data']

        self.match_metadata = {
            'server_name': game_info['Engine.GameReplicationInfo:ServerName'],
            'playlist': game_info['ProjectX.GRI_X:ReplicatedGamePlaylist']
        }

    def _extract_goal_data(self, base_index, search_index=None):
        if not search_index:
            search_index = base_index

        frame = self.replay.netstream[search_index]

        scorer = None

        players = [
            value
            for name, value in frame.actors.items()
            if value['actor_type'] == 'TAGame.Default__PRI_TA'
        ]

        # Figure out who scored.
        for value in players:
            if 'TAGame.PRI_TA:MatchGoals' in value['data']:
                scorer = value['actor_id']
                break

        if not scorer:
            self._extract_goal_data(base_index, search_index - 1)
            return

        self.goal_metadata[base_index] = scorer

    def _get_actors(self):
        for index, frame in self.replay.netstream.items():
            # We can attempt to get the match metadata during this loop and
            # save us having to loop the netstream more than once.
            if not self.match_metadata:
                self._get_match_metadata(frame)

            # Find the player actor objects.
            players = [
                value
                for name, value in frame.actors.items()
                if value['actor_type'] == 'TAGame.Default__PRI_TA'
            ]

            for value in players:
                """
                Example `value`:

                {'actor_id': 2,
                 'actor_type': 'TAGame.Default__PRI_TA',
                 'data': {'Engine.PlayerReplicationInfo:Ping': 24,
                          'Engine.PlayerReplicationInfo:PlayerID': 656,
                          'Engine.PlayerReplicationInfo:PlayerName': "AvD Sub'n",
                          'Engine.PlayerReplicationInfo:Team': (True, 6),
                          'Engine.PlayerReplicationInfo:UniqueId': (1, 76561198040631598, 0),
                          'Engine.PlayerReplicationInfo:bReadyToPlay': True,
                          'TAGame.PRI_TA:CameraSettings': {'dist': 270.0,
                                                           'fov': 107.0,
                                                           'height': 110.0,
                                                           'pitch': -2.0,
                                                           'stiff': 1.0,
                                                           'swiv': 4.300000190734863},
                          'TAGame.PRI_TA:ClientLoadout': (11, [23, 0, 613, 39, 752, 0, 0]),
                          'TAGame.PRI_TA:ClientLoadoutOnline': (11, 0, 0),
                          'TAGame.PRI_TA:PartyLeader': (1, 76561198071203042, 0),
                          'TAGame.PRI_TA:ReplicatedGameEvent': (True, 1),
                          'TAGame.PRI_TA:Title': 0,
                          'TAGame.PRI_TA:TotalXP': 9341290,
                          'TAGame.PRI_TA:bUsingSecondaryCamera': True},
                 'new': False,
                 'startpos': 102988}
                 """

                if 'Engine.PlayerReplicationInfo:PlayerName' not in value['data']:
                    continue

                team_id = None
                actor_id = value['actor_id']

                if 'Engine.PlayerReplicationInfo:Team' in value['data']:
                    team_id = value['data']['Engine.PlayerReplicationInfo:Team'][1]

                if actor_id in self.actors:
                    if (not self.actors[actor_id]['team'] and team_id) or team_id == -1:
                        self.actors[actor_id]['team'] = team_id

                elif 'TAGame.PRI_TA:ClientLoadout' in value['data']:
                    player_name = value['data']['Engine.PlayerReplicationInfo:PlayerName']

                    self.actors[actor_id] = {
                        'type': 'player',
                        'join': index,
                        'left': self.replay.header['NumFrames'],
                        'name': player_name,
                        'team': team_id,
                    }

                    if actor_id not in self.actor_metadata:
                        self.actor_metadata[actor_id] = value['data']

            # Get the ball data (if any).
            ball = [
                value
                for name, value in frame.actors.items()
                if (
                    value['actor_type'] == 'Archetypes.Ball.Ball_Default' and
                    'TAGame.RBActor_TA:ReplicatedRBState' in value.get('data', {})
                )
            ]

            if ball:
                ball = ball[0]

                if ball['actor_id'] not in self.actors and 'TAGame.RBActor_TA:ReplicatedRBState' in ball['data']:
                    self.actors[ball['actor_id']] = {
                        'type': 'ball'
                    }

    def _get_player_position_data(self, player_id):
        player = self.actors[player_id]
        result = {}

        car_actor_obj = None

        if player['type'] == 'player':
            for index in range(player['join'], player['left']):
                try:
                    frame = self.replay.netstream[index]
                except KeyError:
                    # Handle truncated network data.
                    break

                # First we need to find the player's car object.
                for actor in frame.actors:
                    actor_obj = frame.actors[actor]

                    if 'data' not in actor_obj:
                        continue

                    engine = actor_obj['data'].get('Engine.Pawn:PlayerReplicationInfo')

                    # This is the correct object for this player.
                    if engine and engine[1] == player_id:
                        car_actor_obj = actor_obj['actor_id']

                    # If the actor we're looking at is the car object, then get the
                    # position and rotation data for this frame.
                    if actor_obj['actor_id'] == car_actor_obj:
                        state_data = actor_obj['data'].get('TAGame.RBActor_TA:ReplicatedRBState')

                        if state_data:
                            x, y, z = state_data['pos']
                            yaw, pitch, roll = state_data['rot']

                            result[index] = {
                                'x': x,
                                'y': y,
                                'z': z,
                                'pitch': pitch,
                                'roll': roll,
                                'yaw': yaw
                            }

        elif player['type'] == 'ball':
            for index, frame in self.replay.netstream.items():
                # Does this actor exist in the frame data?
                for actor in frame.actors:
                    actor_obj = frame.actors[actor]

                    if 'data' not in actor_obj:
                        continue

                    if actor_obj['actor_id'] != player_id:
                        continue

                    if 'TAGame.RBActor_TA:ReplicatedRBState' not in actor_obj['data']:
                        continue

                    state_data = actor_obj['data']['TAGame.RBActor_TA:ReplicatedRBState']

                    x, y, z = state_data['pos']
                    yaw, pitch, roll = state_data['rot']

                    result[index] = {
                        'x': x,
                        'y': y,
                        'z': z,
                        'pitch': pitch,
                        'roll': roll,
                        'yaw': yaw
                    }

        return result