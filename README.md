# LunaHR - Heart rate to VRChat through OSC 

<img src="app_icon.png" width="200" height="200" />

This fork hopes to be a decent port to Linux of LunaUwU's LunaHR. It's still a work in progress.

At this moment, its unsure if an H9 or other Polar devices would work with the PolarH10 script.

H10 and devices used with Pulsoid are the only confirmed to work at the current moment.
If you have another Polar monitor, please test my script with your device and let me know if it works! <3

[Consider supporting LunaUwU on Ko-Fi :3](https://ko-fi.com/imlunauwu)

## Installs:

Everything should now be straightforward.

Get the Unity prefab here: [Quest (Display only, no pulse anim./audio effects) compatible (34 bytes)](https://github.com/ImLunaUwU/LunaHR/blob/main/LunaHR%20(Uses%20more%20parameters%2C%20but%20works%20with%20quest).unitypackage), [PC only (Optional Heartbeat sound effects + Pulse animation) (10-12 bytes)](https://github.com/ImLunaUwU/LunaHR/blob/main/LunaHR%20(Optimized%20for%20PC%2C%20has%20fancy%20features%2C%20not%20quest%20compatible).unitypackage)

Get the version of the HR software from here: eventually...?

### Avatar
The needed prefabs is in the unitypackages (see links above). Avatar setup is as simple as any other VRCFury asset, and should be drag and drop onto your avatar.

Before importing the unitypackage, please make sure you already have Poiyomi Toon (or Poi Pro) installed.
Alternatively, if you do not want to use Poi, you'd lack the BPM effect unless you set it up yourself.

The PC version now has heartbeat sounds!

Please adjust the volumes to your likings as I turned them up after feedback said they were quiet.

*LunaHR/HR Prefab should be dragged onto the avatar root itself.*

When adding to the avatar, the display defaults to be on your left wrist/left lower arm bone. This can be changed by unpacking the prefab and changing armature link settings.

Remember to have the HR prefab floating a little, as it otherwise would most likely clip into your arm. By default, it is set up for the Rexouium and Rex edits, but can be used on any avatars by moving it to an appropriate position.

VRCFury should take care of all setup from this point.

*The HR Prefab can be left on before uploading, as it now uses VRCFury toggles.*

Feel free to customize materials to your liking.

Because of a nice partnership with Pulsoid, if you sign up for the "BRO" plan through Luna's authentication or my affiliate link (coming soon), you will get a slight discount, and Luna will get a *slight* bit of change for their pocket :3

They've intentionally made it so this deal benefits you (the user) more than themself :3

## Credits and info
HEAVILY inspired by the (now inactive) project here: https://github.com/200Tigersbloxed/HRtoVRChat_OSC/

This project does NOT use the same parameters as the one by 200Tigersbloxed. It does use less though.
This is both because they're not meant to be the same, nor compatible, and also becuase everything in that project is outdated and the Unity files doesn't really work properly anymore.
*Feel free to use this as a (semi-)direct replacement.*

PC only prefab uses the [Simple Counter Shader](https://www.patreon.com/posts/simple-counter-62864361) from [RED_SIM](https://www.patreon.com/red_sim).
