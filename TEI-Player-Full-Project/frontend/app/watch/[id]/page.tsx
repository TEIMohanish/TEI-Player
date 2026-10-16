"use client";

import Hls from "hls.js";
import { useEffect, useRef, useState } from "react";

type Track = { index: number; codec: string; channels?: number };
type MediaInfo = {
  id: string; name: string; size: number; mime_type: string; created_at: string;
  expires_at: string; hls_ready: boolean; duration: number | null;
  width: number | null; height: number | null; video_codec: string | null;
  audio_tracks: Track[]; subtitle_tracks: Track[]; hls_url: string | null; download_url: string;
};
const API = (process.env.NEXT_PUBLIC_API_BASE_URL || "").replace(/\/$/, "");
function bytes(n: number) {
  if (!Number.isFinite(n) || n < 0) return "Unknown";
  const units = ["B","KB","MB","GB","TB"]; let v=n,i=0;
  while(v>=1024 && i<units.length-1){v/=1024;i++;}
  return `${v.toFixed(i?1:0)} ${units[i]}`;
}
function duration(n: number | null) {
  if (!n || !Number.isFinite(n)) return "—";
  const h=Math.floor(n/3600),m=Math.floor((n%3600)/60),s=Math.floor(n%60);
  return h?`${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`:`${m}:${String(s).padStart(2,"0")}`;
}
export default function WatchPage({ params }: { params: { id: string } }) {
  const videoRef=useRef<HTMLVideoElement>(null);
  const [media,setMedia]=useState<MediaInfo|null>(null);
  const [error,setError]=useState("");
  const [remaining,setRemaining]=useState("");
  const [copied,setCopied]=useState(false);

  useEffect(()=>{
    if(!API){setError("Player API is not configured. Add NEXT_PUBLIC_API_BASE_URL in Vercel.");return;}
    let dead=false;
    fetch(`${API}/api/media/${encodeURIComponent(params.id)}`)
      .then(async r=>{if(!r.ok)throw new Error(r.status===410?"This player link has expired.":"Media link not found or unavailable.");return r.json();})
      .then((d:MediaInfo)=>{if(!dead)setMedia(d);})
      .catch(e=>{if(!dead)setError(e.message||"Unable to load media.");});
    return()=>{dead=true;};
  },[params.id]);

  useEffect(()=>{
    if(!media)return;
    const update=()=>{
      const ms=new Date(media.expires_at).getTime()-Date.now();
      if(ms<=0){setRemaining("Expired");return;}
      const total=Math.floor(ms/1000),h=Math.floor(total/3600),m=Math.floor((total%3600)/60);
      setRemaining(h?`${h}h ${m}m left`:`${m}m left`);
    };
    update();const timer=window.setInterval(update,30000);return()=>window.clearInterval(timer);
  },[media]);

  useEffect(()=>{
    const video=videoRef.current;if(!video||!media?.hls_url)return;
    let hls:Hls|null=null;
    if(video.canPlayType("application/vnd.apple.mpegurl"))video.src=media.hls_url;
    else if(Hls.isSupported()){hls=new Hls({enableWorker:true,lowLatencyMode:false});hls.loadSource(media.hls_url);hls.attachMedia(video);}
    else setError("This browser does not support HLS playback.");
    return()=>{hls?.destroy();video.removeAttribute("src");video.load();};
  },[media]);

  async function copyUrl(){
    if(!media?.hls_url)return;
    try{await navigator.clipboard.writeText(media.hls_url);setCopied(true);window.setTimeout(()=>setCopied(false),1800);}
    catch{window.prompt("Copy HLS URL",media.hls_url);}
  }
  function external(){if(media?.hls_url)window.open(media.hls_url,"_blank","noopener,noreferrer");}

  return <main className="watchShell">
    <header className="topbar">
      <a className="brand" href="/"><span className="brandMark">▶</span><span>TEI PLAYER</span></a>
      <div className="secureTag"><span className="tinyDot"/> SHARE LINK PLAYER</div>
    </header>
    {error&&<div className="notice error"><strong>Something went wrong</strong><span>{error}</span></div>}
    {!media&&!error&&<div className="notice">Loading your media…</div>}
    {media&&<>
      <section className="playerFrame">
        <div className="playerChrome"><span className="playerBrand"><span className="tinyDot"/> TEI PLAYER</span><span className="streamBadge">HLS STREAM</span></div>
        {media.hls_ready?<video ref={videoRef} controls playsInline preload="metadata" className="video"/>:
          <div className="unavailable"><div className="bigPlay">▶</div><h2>Playback unavailable</h2><p>This source could not be packaged for HLS without re-encoding. The original file is still available below.</p></div>}
        <div className="playerFoot"><span>{duration(media.duration)}</span><span>{media.width&&media.height?`${media.width} × ${media.height}`:"ORIGINAL SOURCE"}</span></div>
      </section>
      <section className="filePanel">
        <div className="fileHeading"><div className="fileIcon">▤</div><div className="fileTitle"><h1 title={media.name}>{media.name}</h1><p>Original file details</p></div><div className="expiry"><span className="expiryDot"/>{remaining}</div></div>
        <div className="metadata">
          <div><span>FILE SIZE</span><strong>{bytes(media.size)}</strong></div>
          <div><span>VIDEO CODEC</span><strong>{media.video_codec||"Unknown"}</strong></div>
          <div><span>AUDIO TRACKS</span><strong>{media.audio_tracks?.length??0}</strong></div>
          <div><span>SUBTITLES</span><strong>{media.subtitle_tracks?.length??0}</strong></div>
        </div>
        <div className="actions">
          <a className="action primary" href={media.download_url}><span className="actionIcon">↓</span><span><strong>Download Original File</strong><small>Get the uploaded file, unchanged</small></span><span className="actionArrow">↗</span></a>
          <button className="action" onClick={copyUrl} disabled={!media.hls_url}><span className="actionIcon">⧉</span><span><strong>{copied?"HLS URL Copied":"Copy HLS URL"}</strong><small>Copy stream address to clipboard</small></span><span className="actionArrow">↗</span></button>
          <button className="action" onClick={external} disabled={!media.hls_url}><span className="actionIcon">▷</span><span><strong>External Players</strong><small>Open in VLC, MX Player or another app</small></span><span className="actionArrow">↗</span></button>
        </div>
      </section>
    </>}
    <footer><span>© 2026 TEI Player. All rights reserved.</span><span>MADE FOR YOUR MEDIA</span></footer>
  </main>;
}
