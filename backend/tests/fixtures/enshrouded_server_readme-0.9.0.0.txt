Stripped copy of Keen's enshrouded_server_readme.txt for tests: only the parts
FlameKeeper's parser reads (version, keys, defaults, ranges, options, tags and the
default config). The descriptions are left out.

Version: 0.9.0.0

SERVER SETTINGS
name
saveDirectory
logDirectory
ip
queryPort
slotCount
tags
voiceChatMode
enableVoiceChat
enableTextChat
gameSettingsPreset
userGroups
bans
List of available tags:
		LookingForPlayers,
		English,
		German,
		French,
		Italian,
		Japanese,
		Korean,
		Polish,
		Portuguese,
		Russian,
		Spanish,
		Thai,
		Turkish,
		Ukrainian,
		Chinese,
		Taiwanese,
		BaseBuilding,
		Exploration,
		Roleplay,

PERMISSION SETTINGS

Player Health
"gameSettings": {
"playerHealthFactor": 1,
}
Min value: 0.25
Max value: 4


Player Mana
"gameSettings": {
"playerManaFactor": 1,
}
Min value: 0.25
Max value: 4


Player Stamina
"gameSettings": {
"playerStaminaFactor": 1,
}
Min value: 0.25
Max value: 4


Player body heat against cold weather
"gameSettings": {
"playerBodyHeatFactor": 1,
}
Min value: 0.5
Max value: 2


NEW! Diving time
"gameSettings": {
"playerDivingTimeFactor": 1,
}
Min value: 0.5
Max value: 2


Weapon Durability
"gameSettings": {
"enableDurability": true,
}
Options: true / false


Hunger and Starvation
"gameSettings": {
"enableStarvingDebuff": false,
}
Options: true / false


Food Buff Duration
"gameSettings": {
"foodBuffDurationFactor": 1,
}
Min value: 0.5
Max value: 2


Hungry State Duration
"gameSettings": {
"fromHungerToStarving": 600000000000,
}
Min value: 5 minutes
Max value: 20 minutes


Shroud time duration modifier
"gameSettings": {
"shroudTimeFactor": 1,
}
Min value: 0.5 
Max value: 2


Tombstone Mode
"gameSettings": {
"tombstoneMode": "AddBackpackMaterials",
}
Options: AddBackpackMaterials / Everything / NoTombstone 


Air turbulences while gliding
"gameSettings": {
"enableGliderTurbulences": true,
}
Options: true / false


Weather phenomena frequency
"gameSettings": {
"weatherFrequency": "Normal",
}
Options: Disabled / Rare / Normal / Often


NEW! Fishing difficulty
"gameSettings": {
"fishingDifficulty": "Normal",
}
Options: VeryEasy, Easy, Normal, Hard, VeryHard


Mining Effectiveness
"gameSettings": {
"miningDamageFactor": 1,
}
Min value: 0.5 
Max value: 2


Plant Growth Speed
"gameSettings": {
"plantGrowthSpeedFactor": 1,
}
Min value: 0.25 
Max value: 2


Resources Gain Modifier
"gameSettings": {
"resourceDropStackAmountFactor": 1,
}
Min value: 0.25 
Max value: 2


Workstation Effectiveness
"gameSettings": {
"factoryProductionSpeedFactor": 1,
}
Min value: 0.25 
Max value: 2


Weapon Recycling Yield Modifier
"gameSettings": {
"perkUpgradeRecyclingFactor": 0.100000,
}
Min value: 0 
Max value: 1


Weapon Upgrading Costs
"gameSettings": {
"perkCostFactor": 1,
}
Min value: 0.25 
Max value: 2


Combat Experience Modifier
"gameSettings": {
"experienceCombatFactor": 1,
}
Min value: 0.25 
Max value: 2


Mining Experience Modifier
"gameSettings": {
"experienceMiningFactor": 1,
}
Min value: 0 
Max value: 2


Exploration Experience Modifier
"gameSettings": {
"experienceExplorationQuestsFactor": 1,
}
Min value: 0.25 
Max value: 2


Enemy Amount
"gameSettings": {
"randomSpawnerAmount": "Normal",
}
Options: Few / Normal / Many / Extreme 


Modifier for simultaneous Enemy Attacks
"gameSettings": {
"aggroPoolAmount": "Normal",
}
Options: Few / Normal / Many / Extreme


Enemy Damage
"gameSettings": {
"enemyDamageFactor": 1,
}
Min value: 0.25 
Max value: 5


Enemy Health
"gameSettings": {
"enemyHealthFactor": 1,
}
Min value: 0.25 
Max value: 4


Enemy Stun Modifier
"gameSettings": {
"enemyStaminaFactor": 1,
}
Min value: 0.5 
Max value: 2


Enemy Perception Modifier
"gameSettings": {
"enemyPerceptionRangeFactor": 1,
}
Min value: 0.5 
Max value: 2


Boss Damage Modifier
"gameSettings": {
"bossDamageFactor": 1,
}
Min value: 0.2 
Max value: 5


Boss Health Modifier
"gameSettings": {
"bossHealthFactor": 1,
}
Min value: 0.2 
Max value: 5


Enemy Attacks Modifier
"gameSettings": {
"threatBonus": 1,
}
Min value: 0.25
Max value: 4


Pacified Enemies
"gameSettings": {
"pacifyAllEnemies": false,
}
Options: true / false


Taming failure repercussions
"gameSettings": {
"tamingStartleRepercussion": "LoseSomeProgress",
}
Options: KeepProgress / LoseSomeProgress / LoseAllProgress


Daytime
"gameSettings": {
"dayTimeDuration": 1800000000000,
}
Min value: 2 minutes
Max value: 60 minutes


Nighttime
"gameSettings": {
"nightTimeDuration": 720000000000,
}
Min value: 2 minutes
Max value: 60 minutes


Modify Shroud curse
"gameSettings": {
"curseModifier": "Normal",
}
Options: Easy (which turns the feature off) / Normal / Hard (which doubles the chance relative to normal)


DEFAULT enshrouded_server.json / VERSION 0.9.0.0

{
	"name": "Enshrouded Server",
	"saveDirectory": "./savegame",
	"logDirectory": "./logs",
	"ip": "0.0.0.0",
	"queryPort": 15637,
	"slotCount": 16,
	"tags": [
	],
	"voiceChatMode": "Proximity",
	"enableVoiceChat": false,
	"enableTextChat": false,
	"gameSettingsPreset": "Default",
	"gameSettings": {
		"playerHealthFactor": 1,
		"playerManaFactor": 1,
		"playerStaminaFactor": 1,
		"playerBodyHeatFactor": 1,
		"playerDivingTimeFactor": 1,
		"enableDurability": true,
		"enableStarvingDebuff": false,
		"foodBuffDurationFactor": 1,
		"fromHungerToStarving": 600000000000,
		"shroudTimeFactor": 1,
		"tombstoneMode": "AddBackpackMaterials",
		"enableGliderTurbulences": true,
		"weatherFrequency": "Normal",
		"fishingDifficulty": "Normal",
		"miningDamageFactor": 1,
		"plantGrowthSpeedFactor": 1,
		"resourceDropStackAmountFactor": 1,
		"factoryProductionSpeedFactor": 1,
		"perkUpgradeRecyclingFactor": 0.500000,
		"perkCostFactor": 1,
		"experienceCombatFactor": 1,
		"experienceMiningFactor": 1,
		"experienceExplorationQuestsFactor": 1,
		"randomSpawnerAmount": "Normal",
		"aggroPoolAmount": "Normal",
		"enemyDamageFactor": 1,
		"enemyHealthFactor": 1,
		"enemyStaminaFactor": 1,
		"enemyPerceptionRangeFactor": 1,
		"bossDamageFactor": 1,
		"bossHealthFactor": 1,
		"threatBonus": 1,
		"pacifyAllEnemies": false,
		"tamingStartleRepercussion": "LoseSomeProgress",
		"dayTimeDuration": 1800000000000,
		"nightTimeDuration": 720000000000,
		"curseModifier": "Normal"
	},
	"userGroups": [
		{
			"name": "Admin",
			"password": "AdminXXXXXXXX",
			"canKickBan": true,
			"canAccessInventories": true,
			"canEditWorld": true,
			"canEditBase": true,
			"canExtendBase": true,
			"reservedSlots": 0
		},
		{
			"name": "Friend",
			"password": "FriendXXXXXXXX",
			"canKickBan": false,
			"canAccessInventories": true,
			"canEditWorld": true,
			"canEditBase": true,
			"canExtendBase": false,
			"reservedSlots": 0
		},
		{
			"name": "Guest",
			"password": "GuestXXXXXXXX",
			"canKickBan": false,
			"canAccessInventories": false,
			"canEditWorld": true,
			"canEditBase": false,
			"canExtendBase": false,
			"reservedSlots": 0
		},
		{
			"name": "Visitor",
			"password": "VisitorXXXXXXXX",
			"canKickBan": false,
			"canAccessInventories": false,
			"canEditWorld": false,
			"canEditBase": false,
			"canExtendBase": false,
			"reservedSlots": 0
		}
	],
	"bans": [
	]
}
