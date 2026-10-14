# AI USAGE DISCLOSURE:

TLDR: AI was used in this project in moderate amounts. Some code is written by it, but it is mostly by my own hand, and thoroughly reviewed when not. I use it to better understand and attempt to scale this huge project. Art (like the logos) never is and never will be AI generated. Code is my own, except for one instance, which has approval from the original owner. More detail below.

## Q) Was AI used in this project in any capacity?
Yes. AI was used in this project. I do not consider it vibe coded, nor is it a quick "anything counts" project. AI was used mostly in a way to make this project understandable for most people and myself. How? Commenting, small fixes and unit tests. The first and third uses are some that I mostly regret now, since they are useless for anyone other than the LLM itself.

## Q) How was AI used?

Let's take a real example. I wanted to add text markers to the Fortress of Doom Battery Rooms. I wanted to find a way to add text in-game, and I remembered that months ago I watched a vídeo about a mod and it had floating text. Therefore, I knew it existed. I consulted the doom modding wiki, studied the String Modding page and started implementing it. Then, when I made it work, I added a function to create strings per room content. 

After all was said and done, and a working prototype existed, I asked Codex to review it, find weak points, and give me some tips to fix things.

That's it. I (almost) never let it write code itself, and when it did, it was always thoroughly reviewed. Furthermore, when a version was considered "finished", I asked it to code review again. So some things may not be right because Codex isn't always right, nor am I. 

This usage mostly stems from not being confident enough in creating a large scale project like that by myself. I do work as a dev full-time, but this is my first big, community project and I wanted a higher level of quality. One that for now, AI helps me to achieve.

## ADDENDUM: 
My usage increased recently (September 2026), since I switced jobs and time is now scarce. This increased usage is mostly "fix this while I do that" or "this is bugging out, what do I do?". That also includes the support report logs users send me when there's something wrong in the mod. Codex looks at the logs (all of them are stripped of sensitive information, but there wouldn't be any in any case), tells me what happened and I can then plan a fix.

Also, since v0.6 was a big step up from previous versions because of the new DLL required, that one used more AI than the original mod itself, since I barely understand reverse engineering. I do know C++ enough to work with it, but Ghidra helped me a lot. Is it doable without AI? Yes, very much so, it was a matter of time and knowledge mostly.

NO BIG DECISION IS MADE BY AI. I played more than 200 hours of Eternal SINCE THIS MOD WAS CREATED, simply testing everything as far as I can. There's hundred more hours in research as well. Most things were covered by the community sources, but some were so obscure I had to find a way myself.

As said before: if this increased, moderate usage, is something that bothers you, you are free to stick with v0.5.3. 

AI was also used in the README file to organize it. English isn't my first language, this file is all human tho.

## Q) Do you own all of your code?

Technically, not 100% of it. Meathook, the old DLL used, and created by the amazing chrispy, got obsolete and I had to replace it with a DLL of my own. Meathook did have several useful features that I couldn't afford to lose, so I asked chrispy publicaly and in their DMs if I could modify and publish it and they gave me permission, so Meathook's RPC feature is included inside Sentinel Core.

Besides that, NO CODE was copied from other mods. When I credit other moders is simply from a "is it doable?" standpoint, like seeing if something is actually possible and viable with the tools I have. Even if the code is open source and licensed, I don't really like the idea of copying stuff.

## Q) Your mod has various logos and art assets. Are they AI generated too?

NOPE! Never was and never will be. Not even for testing purposes. Every single version uses art made by the community (huge shoutout to FridgeDuck from the AP AD Discord) or comissioned by me. Even if there's not art available, I strongly believe in doing something, even if scuffed. My views from AI usage in code are NOT the same as AI usage in art. I don't want no part in AI generated "art", and do not approve of it.

## Q) What AI tools did you use?

- Codex
- Serena (MCP server for Codex)
- Ghidra (RE tool, used only for v0.6+)

## Q) One or more functions or code decisions are not optimal. Is that the LLM or you?

Mostly, when something doesn't make a lick of sense, it was me. I catch some mistakes once in a while and I'm always glad when its pointed out. From this file being long as hell, you can also see I love going overboard sometimes. That constantly comes back to bite me in the ass.

## Q) You keep making lots of changes in a short time. How?

I really do work in this project more than I should. Like from when I wake up until I go to sleep (very late by the way). I'm not exaggerating in any way when I say I work every single hour of the day in this. Even during work hours, even when I'm not home. I also know this isn't exactly healthy. I have never felt so good working with software as I am working in this right now. From having people constantly playing and helping me out (like Doctor Pyro, Quartz, Zeforas and so many others) to watching high profile streamers playing it, it's a blessing to be able to take part in Archipelago like this.

I hope this document may be of help. Thanks for reading.
