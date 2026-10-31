# Possible New Features

## Next up (decided 2026-10-31, not built yet)

**Shorts in real HD.** Shorts look bad on YouTube: the landscape meeting is
shrunk into a thin strip across the middle of the 1080x1920 frame (half the
screen is a blurred copy), so slide text is a few pixels tall, and the
picture is soft. Burned-in titles and captions are sharp, so it is the
picture itself. Plan:

1. Zoom in for shorts: crop the frame to its middle part (drop the browser
   bar, slide thumbnails, edges) and scale it up to fill much more of the
   height (~1.5-2x bigger). Make the crop amount a setting.
2. Encode shorts with their own HD settings (~8 Mbps, crf 18-20, slower
   preset, lanczos scaling). Today every video uses crf 24 veryfast capped
   at 2.5 Mbps (slice/profile.py). The full video can keep its settings.
3. Check the source: ask a Zoom admin to turn on HD (720p/1080p) cloud
   recording; the code can't add detail Zoom never recorded. First check a
   job's source.mp4 resolution with ffprobe.

Test on a synthetic screen-share clip and compare before/after frames before
pushing.

Running list of ideas to consider later. Nothing here is implemented yet — add to this
file as new ideas come up.

- Add a short summary/overview of the whole meeting at the start of the output video.
- Add timestamps for users (so they can see where in the original recording each part came from).
- Maybe help marketing by gathring funny clips or higlighted clips  
- Make an automation where the summary or the  whole transcript is store in a vector DB and when someone misses a meeting he can just chat via a chatbot to learn about specific stuff  This would save alot  of time 


how to fix transiastions 

seperate noise vedio with time stamps 

add higlisht amybe 4-5 mins  to the beigning 

also make some reels tuff form that  
we need to do all nthis in 1 go   
make intro + highlghts +   cleaned vedio and shrot clips   

would need an api key    , zooms api key   ,  a member from   content creation team  


