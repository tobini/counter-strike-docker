import collections
import datetime
import logging
import typing
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Collection, Dict, Iterable, Iterator, List, Mapping, Optional, TYPE_CHECKING, Tuple

from frozendict import frozendict

from log_parser.entity import CT_team, Player, Team, Terrorist_team, Weapon
from log_parser.event import Event, KillEvent


@dataclass
class PlayerStats:
    damage_inflicted: int = 0
    damage_received: int = 0
    kills: int = 0
    deaths: int = 0
    rounds_won: int = 0
    rounds_lost: int = 0
    time_spent: datetime.timedelta = datetime.timedelta(0)
    damage_inflicted_by_weapon: Dict['Weapon', int] = field(default_factory=lambda: collections.defaultdict(int))

    def time_spent_in_seconds(self) -> float:
        return self.time_spent.total_seconds()

    def time_spent_in_hours(self) -> float:
        return self.time_spent_in_seconds() / 3600

    def total_rounds_played(self) -> int:
        return self.rounds_won + self.rounds_lost


class RoundReport:
    def __init__(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        events: List[Event],
        team_composition: Dict[Team, List[Player]],
        winner_team: Optional[Team],
    ):
        self._start_time = start_time
        self._end_time = end_time
        # copy and freeze the mutable objects
        self._events: Tuple[Event, ...] = tuple(events)
        self._team_composition: Mapping[Team, Tuple[Player, ...]] = frozendict(
            {team: tuple(players) for team, players in team_composition.items()}
        )
        self._winner_team = winner_team

    def get_start_time(self) -> datetime.datetime:
        return self._start_time

    def get_end_time(self) -> datetime.datetime:
        return self._end_time

    def get_round_duration(self) -> datetime.timedelta:
        return self._end_time - self._start_time

    def get_events(self) -> Tuple[Event, ...]:
        return self._events

    def get_team_composition(self, team: Team) -> Tuple[Player, ...]:
        return self._team_composition[team]

    def get_ct_team_composition(self) -> Tuple[Player, ...]:
        return self.get_team_composition(CT_team)

    def get_terrorist_team_composition(self) -> Tuple[Player, ...]:
        return self.get_team_composition(Terrorist_team)

    def get_winner_team(self) -> Optional[Team]:
        return self._winner_team

    def get_all_players(self) -> Tuple[Player, ...]:
        # ignore spectators
        return self.get_ct_team_composition() + self.get_terrorist_team_composition()

    def get_player_stats(self, player: Player) -> 'PlayerStats':
        assert player in self.get_all_players()
        stats = PlayerStats()
        self.add_to_player_stats(player, stats)
        return stats

    def add_to_player_stats(self, player: Player, stats: PlayerStats) -> None:
        if player in self.get_all_players():
            for event in self.get_events():
                event.impact_player_stats(player, stats, self)
            # ignore middle-round disconnections
            duration = self.get_round_duration()
            stats.time_spent += duration


class MatchReport:
    """
    An inmutable representation of an ended match.
    A match is a sequence of rounds happening in a given map.
    Every log file contains one match.
    """

    def __init__(self, match_events: List[Event], map_name: str, rounds: List[RoundReport]):
        self._match_events = match_events
        self._map_name = map_name
        self._round_reports = tuple(rounds)  # make inmutable

    def get_round_reports(self) -> Tuple[RoundReport, ...]:
        return self._round_reports

    def get_first_attack(self) -> Event:
        for event in self._all_round_events():
            if event.is_attack():
                return event
        raise Exception("There's no blood in this game")

    def get_first_kill(self) -> KillEvent:
        return next(iter(self.get_all_kills()))

    def get_all_kills(self) -> Iterable[KillEvent]:
        for event in self._all_round_events():
            if event.is_kill():
                assert isinstance(event, KillEvent)
                yield event

    def get_map_name(self) -> str:
        return self._map_name

    def get_start_time(self) -> datetime.datetime:
        first_event = self._match_events[0]
        start_time = first_event.get_timestamp()
        return start_time

    def get_end_time(self) -> datetime.datetime:
        last_event = self._match_events[-1]
        end_time = last_event.get_timestamp()
        return end_time

    def get_rounds_by_winner_team(self) -> Dict[Team, List[RoundReport]]:
        rounds_by_winner_team: Dict[Team, List[RoundReport]] = {
            CT_team: [],
            Terrorist_team: [],
        }
        for round_report in self.get_round_reports():
            winner_team = round_report.get_winner_team()
            if winner_team:
                rounds_by_winner_team[winner_team].append(round_report)
        return rounds_by_winner_team

    def get_scores(self) -> Dict[Team, int]:
        return {team: len(rounds_won) for team, rounds_won in self.get_rounds_by_winner_team().items()}

    def get_team_score(self, team: Team) -> int:
        return self.get_scores()[team]

    def get_player_stats(self, player: Player) -> PlayerStats:
        stats = PlayerStats()
        self.add_to_player_stats(player, stats)
        return stats

    def get_all_player_stats(self) -> Dict[Player, PlayerStats]:
        return {player: self.get_player_stats(player) for player in self.get_all_players()}

    def add_to_player_stats(self, player: Player, stats: PlayerStats) -> None:
        for round in self.get_round_reports():
            round.add_to_player_stats(player, stats)

    def add_to_player_stats_table(self, table: Dict[Player, PlayerStats]) -> None:
        for player in self.get_all_players():
            stats = table[player]
            self.add_to_player_stats(player, stats)

    def get_all_players(self) -> Iterable[Player]:
        return {player for round in self.get_round_reports() for player in round.get_all_players()}

    def _all_round_events(self) -> Iterator[Event]:
        return (event for round in self.get_round_reports() for event in round.get_events())


PlayerTable = Dict[Player, PlayerStats]

if TYPE_CHECKING:
    BaseClass = typing.Iterable[MatchReport]
else:
    BaseClass = collections.Iterable


class MatchReportCollection(BaseClass):
    """
    A collection of ended matches.
    Useful to cache some stats.
    """

    def __init__(self, match_reports: Collection[MatchReport]) -> None:
        self._match_reports = match_reports

    @lru_cache(maxsize=None)
    def collect_stats(self) -> PlayerTable:
        stats_by_player: PlayerTable = collections.defaultdict(PlayerStats)
        for report in self._match_reports:
            report.add_to_player_stats_table(stats_by_player)
            logging.debug(f"Stats collected for match {report}")
        return stats_by_player

    def get_total_number_of_rounds(self):
        return sum(len(match.get_round_reports()) for match in self._match_reports)

    def get_sorted_score_table(self, scorer: 'ScorerStrategy') -> List[Tuple[Player, float]]:
        scores = scorer.get_player_scores(self)
        sorted_score_table = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return sorted_score_table

    def get_best_player(self, scorer: 'ScorerStrategy') -> Tuple[Player, float]:
        score_table = self.get_sorted_score_table(scorer)
        return score_table[0]

    def get_full_player_scores(self, scorer: 'ScorerStrategy') -> Mapping[Player, 'FullScore']:
        return scorer.get_full_player_scores(self)

    def __iter__(self) -> Iterator[MatchReport]:
        return iter(self._match_reports)

    def __len__(self) -> int:
        return len(self._match_reports)
