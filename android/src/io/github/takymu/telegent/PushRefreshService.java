package io.github.takymu.telegent;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** Small persisted token maintenance job; no permanent polling service or main account token. */
public final class PushRefreshService extends JobService {
    private volatile boolean stopped;
    private boolean discover(SharedPreferences p,String binding){
        if(!NavigationPolicy.DEFAULT_ENTRY.equals(getSharedPreferences("connection",MODE_PRIVATE).getString("entry",NavigationPolicy.DEFAULT_ENTRY)))return false;
        try{
            HttpURLConnection source=(HttpURLConnection)new URL(NavigationPolicy.DEFAULT_ENTRY+"url.txt").openConnection();
            String candidate;
            try{
                source.setInstanceFollowRedirects(false);source.setConnectTimeout(10000);source.setReadTimeout(10000);
                if(source.getResponseCode()!=200)return false;
                java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream();
                try(java.io.InputStream stream=source.getInputStream()){for(int b;(b=stream.read())!=-1&&bytes.size()<512;)bytes.write(b);}
                candidate=new String(bytes.toByteArray(),StandardCharsets.UTF_8).trim();
            }finally{source.disconnect();}
            if(!NavigationPolicy.discoveredServer(candidate)||candidate.equals(p.getString("endpoint",null)))return false;
            HttpURLConnection check=(HttpURLConnection)new URL(NavigationPolicy.origin(candidate)+"/api/push/identity").openConnection();
            try{
                check.setInstanceFollowRedirects(false);check.setConnectTimeout(10000);check.setReadTimeout(10000);
                if(check.getResponseCode()!=200)return false;
                java.io.ByteArrayOutputStream bytes=new java.io.ByteArrayOutputStream();
                try(java.io.InputStream stream=check.getInputStream()){for(int b;(b=stream.read())!=-1&&bytes.size()<1024;)bytes.write(b);}
                if(!p.getString("server_id","").equals(new JSONObject(new String(bytes.toByteArray(),StandardCharsets.UTF_8)).optString("server_id")))return false;
            }finally{check.disconnect();}
            if(!binding.equals(p.getString("binding","")))return false;
            p.edit().putString("endpoint",NavigationPolicy.origin(candidate)).apply();return true;
        }catch(Exception error){return false;}
    }
    static void schedule(Context context){
        context.getSystemService(JobScheduler.class).schedule(new JobInfo.Builder(6402,new ComponentName(context,PushRefreshService.class))
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPersisted(true).setBackoffCriteria(30000,JobInfo.BACKOFF_POLICY_EXPONENTIAL).build());
    }
    @Override public boolean onStartJob(JobParameters parameters){
        stopped=false;
        new Thread(()->{
            SharedPreferences p=NativePush.preferences(this);String endpoint=p.getString("endpoint",null),key=p.getString("device_key",null);
            String operation=p.getString("job","refresh"),binding=p.getString("binding","");boolean retry=false;
            if(endpoint!=null&&key!=null&&NavigationPolicy.origin(endpoint)!=null)try{
                boolean remove="remove".equals(operation)||!p.getBoolean("wanted",false);
                JSONObject body=new JSONObject().put("device_key",key).put("binding",binding);
                if(!remove){String token=p.getString("fcm_token",null);if(token==null)throw new IllegalStateException();body.put("fcm_token",token);}
                HttpURLConnection c=(HttpURLConnection)new URL(NavigationPolicy.origin(endpoint)+"/api/push/devices/"+(remove?"remove":"refresh")).openConnection();
                try{
                    c.setInstanceFollowRedirects(false);c.setConnectTimeout(15000);c.setReadTimeout(15000);c.setRequestMethod("POST");c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json");
                    byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);c.setFixedLengthStreamingMode(bytes.length);
                    try(java.io.OutputStream output=c.getOutputStream()){output.write(bytes);}
                    int status=c.getResponseCode();retry=status>=500||status==429||status==404||status==403;
                    if(status==410&&binding.equals(p.getString("binding","")))p.edit().putBoolean("registered",false).apply();
                }finally{c.disconnect();}
            }catch(Exception error){retry=true;}
            if(retry)discover(p,binding);
            if(!stopped)jobFinished(parameters,retry);
        },"push-token-refresh").start();
        return true;
    }
    @Override public boolean onStopJob(JobParameters parameters){stopped=true;return true;}
}
