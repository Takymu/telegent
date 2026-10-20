package io.github.takymu.telegent;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.net.URLConnection;
import java.util.UUID;

/** Temporary read grants for explicitly shared attachments, never arbitrary app files. */
public final class SharedFilesProvider extends ContentProvider {
    private static final String AUTHORITY="io.github.takymu.telegent.shared";
    @Override public boolean onCreate(){return true;}
    static Uri prepare(Context context,File source,String name)throws IOException {
        String id=UUID.randomUUID().toString();File directory=new File(context.getCacheDir(),"shares/"+id);
        if(!directory.mkdirs())throw new IOException("No share directory");
        name=NavigationPolicy.downloadName(null,name);
        if(!source.renameTo(new File(directory,name)))throw new IOException("Could not prepare share");
        return new Uri.Builder().scheme("content").authority(AUTHORITY).appendPath(id).appendPath(name).build();
    }
    static void cleanup(Context context) {
        File root=new File(context.getCacheDir(),"shares");File[] directories=root.listFiles();
        if(directories==null)return;
        for(File directory:directories)if(directory.isDirectory()&&directory.getName().matches("[0-9a-f-]{36}")
                &&System.currentTimeMillis()-directory.lastModified()>24*3600*1000L){
            File[] files=directory.listFiles();if(files!=null)for(File file:files)if(file.isFile())file.delete();
            directory.delete();
        }
    }
    private File file(Uri uri)throws FileNotFoundException {
        if(!"content".equals(uri.getScheme())||!AUTHORITY.equals(uri.getAuthority())||uri.getPathSegments().size()!=2
                ||uri.getQuery()!=null||uri.getFragment()!=null)throw new FileNotFoundException("Unknown share URI");
        String id=uri.getPathSegments().get(0),name=uri.getPathSegments().get(1);
        if(!id.matches("[0-9a-f-]{36}")||!name.equals(NavigationPolicy.downloadName(null,name)))throw new FileNotFoundException("Invalid share path");
        File directory=new File(getContext().getCacheDir(),"shares/"+id),result=new File(directory,name);
        try{if(!result.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile()))throw new IOException("Invalid share path");}
        catch(IOException error){throw new FileNotFoundException("Invalid share path");}
        return result;
    }
    @Override public String getType(Uri uri){try{String type=URLConnection.guessContentTypeFromName(file(uri).getName());return type==null?"application/octet-stream":type;}catch(FileNotFoundException error){return null;}}
    @Override public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException {
        if(!"r".equals(mode))throw new SecurityException("Read only");
        return ParcelFileDescriptor.open(file(uri),ParcelFileDescriptor.MODE_READ_ONLY);
    }
    @Override public Cursor query(Uri uri,String[] projection,String selection,String[] args,String sort) {
        try{
            File file=file(uri);String[] columns=projection==null?new String[]{OpenableColumns.DISPLAY_NAME,OpenableColumns.SIZE}:projection;
            MatrixCursor cursor=new MatrixCursor(columns);Object[] row=new Object[columns.length];
            for(int i=0;i<columns.length;i++){if(OpenableColumns.DISPLAY_NAME.equals(columns[i]))row[i]=file.getName();else if(OpenableColumns.SIZE.equals(columns[i]))row[i]=file.length();}
            cursor.addRow(row);return cursor;
        }catch(FileNotFoundException error){return null;}
    }
    @Override public Uri insert(Uri uri,ContentValues values){throw new SecurityException("Read only");}
    @Override public int update(Uri uri,ContentValues values,String selection,String[] args){throw new SecurityException("Read only");}
    @Override public int delete(Uri uri,String selection,String[] args){throw new SecurityException("Read only");}
}
