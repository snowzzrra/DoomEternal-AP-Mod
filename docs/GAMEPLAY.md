# Gameplay and options

Choose options under **CREATE PLAYER** before generation. During a session,
**VIEW ALL SETTINGS** shows the options chosen for your room.

## Missions and victory

**Mission Pool** chooses eligible missions; **Mission Count** selects how many to play.
**Vanilla Order** uses their original order. **Random Mission Order** shuffles the
sequence. **Mission Access as Items** lets you enter a mission after finding its
Access item. Completed missions can be replayed from the Fortress of Doom.

With the Enemy Randomizer enabled, Random Mission Order ranks the generated fights
by their enemy composition, attributes and wave pressure. Equipment preparation
and mandatory boss requirements contribute to difficulty; the configured goal stays last.

**Use DLC Content** controls DLC gear and enemy types separately from the mission
selection. Choose your **Goal** and any **Additional Victory Requirements** before
generation. The launcher shows the tasks you still need during play.

**Campaign Difficulty** controls the game's difficulty. **Logic Difficulty** controls
the equipment Archipelago expects you to have before a fight. These are separate choices.

## Enemies

**Enemy Preset** supplies starting values for the Enemy Randomizer. Slayer's Path
offers a balanced campaign; Hell's Vanguard favors harder fights. Adjust weights,
attributes or special enemy limits to customize a preset.

**Enemy Count: Multiplier** scales the original count: 200% requests twice as many
enemies. **Auto** preserves at least the original count of each request and wave.
**Density** can add bodies; **Intensity** changes the preference for stronger enemies.
Species weights still apply throughout the fight. **Progressive Difficulty**
varies the generated composition between early and late bands before mission ranking.

Health, damage, speed and size ranges use percentages: 100% is the usual value.
Equal minimum and maximum values fix an attribute. An individual enemy's setting
overrides the global range. Some scripted enemies and bosses keep their native behavior.

**Special Enemy Limits** caps simultaneous randomized Marauders, Arch-viles,
Spirits, Buff Totems and Screechers. Totems and Screechers share the Arch-vile
limit and the combined special limit. Setting a weight to zero excludes that
species from random picks.

Secret Encounter timers scale with their randomized enemies and include 25%
extra time, with at least five extra seconds. **Easier Secret Encounters** adds
another 50%. Slayer Gate indicators show key ownership and
turn green after the server confirms completion.

## Kill rewards

**Killsanity** adds rewards at kill-counter targets. **Kills per Check: 100** gives
targets at 100, 200, 300 and so on. The counter includes observed deaths from traps,
respawns and mission replays. It records physical deaths rather than multiplying
a kill by the number of enemies it replaced.
The HUD displays the current total and next target below **Items Found**.

**Kill Rewards: Auto** estimates campaign kills, adds 10%, divides by Kills per Check
and rounds down, up to 50 rewards. A fixed reward count can require mission replays.
The estimate is conservative and does not guarantee that one playthrough reaches
every target. Rewards beyond the reviewed finite budget cannot hold progression items.

The native death hook and recovery behavior have offline tests. Testing still needs
to confirm death coverage across enemy families and full campaigns in the game.

## Items, traps and recovery

**Trap Percentage** replaces extra filler rewards with selected traps. Empty
**Enabled Traps** uses helpful consumables instead. Received temporary effects
activate during safe gameplay; receiving another copy extends their duration.

**DeathLink** shares deaths with other enabled players. **Soft** allows Extra Lives
or Saving Throw to protect you. **Hardcore** causes death and checkpoint reload
without consuming those protections.

If an owned item appears missing, load a level and choose **RESYNC INVENTORY**.
Select the scope, return to DOOM and unpause within 30 seconds. The repair waits
for safe gameplay and cancels if you change room, save or level. It restores items
confirmed missing and leaves unknown ownership alone.

**Reveal AP Locations on Automap** helps locate AP pickups. Fast Travel becomes
available when replaying a completed mission.
