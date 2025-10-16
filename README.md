# LunaHR - Heart rate to VRChat through OSC 

<img src="app_icon.png" width="200" height="200" />

This fork hopes to be a decent port to Linux of LunaUwU's [LunaHR](https://github.com/ImLunaUwU/LunaHR). It's still a work in progress.

Polar H10 devices are the only devices that will work with this version.

[Consider supporting LunaUwU on Ko-Fi :3](https://ko-fi.com/imlunauwu)

## Installs:

uhhh... instructions coming soon maybe... probably... maybe...

### Avatar
The needed prefabs are in the unitypackages (see links above). Avatar setup is as simple as any other VRCFury asset, and should be able to drag and drop onto your avatar.

Before importing the unitypackage, please make sure you already have Poiyomi Toon (or Poi Pro) installed.
Alternatively, if you do not want to use Poi, you'd lack the BPM effect unless you set it up yourself.

The PC version now has heartbeat sounds!

*LunaHR/HR Prefab should be dragged onto the avatar root itself.*

When adding to the avatar, the display defaults to be on your left wrist/left lower arm bone. This can be changed by unpacking the prefab and changing armature link settings.

Remember to have the HR prefab floating a little, as it otherwise would most likely clip into your arm. By default, it is set up for the Rexouium and Rex edits, but can be used on any avatars by moving it to an appropriate position.

VRCFury should take care of all setup from this point.

*The HR Prefab can be left on before uploading, as it now uses VRCFury toggles.*

Feel free to customize materials to your liking.

## Credits and info (copied from main branch page)
HEAVILY inspired by the (now inactive) project here: https://github.com/200Tigersbloxed/HRtoVRChat_OSC/

This project does NOT use the same parameters as the one by 200Tigersbloxed. It does use less though.
This is both because they're not meant to be the same, nor compatible, and also becuase everything in that project is outdated and the Unity files doesn't really work properly anymore.
*Feel free to use this as a (semi-)direct replacement.*

PC only prefab uses the [Simple Counter Shader](https://www.patreon.com/posts/simple-counter-62864361) from [RED_SIM](https://www.patreon.com/red_sim).
