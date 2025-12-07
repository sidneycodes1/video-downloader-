from flask import Flask, request, send_file, render_template
import yt_dlp
import os

app = Flask(__name__)

@app.route("/")
def home():
    return render_template('index.html')

@app.route("/download", methods=["GET", "POST"])
def download():
    if request.method == "GET":
        return render_template('index.html', 
                             message="Please use the form to submit a video link", 
                             error=True)
    
    try:
        url = request.form.get('url')
        platform = request.form.get('platform', 'youtube')
        
        if not url:
            return render_template('index.html', 
                                 message="Please provide a video URL", 
                                 error=True)
        
        # Use /tmp directory (only writable location on Vercel)
        downloads_path = '/tmp'
        
        # Configure yt-dlp options with better compatibility
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(downloads_path, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'socket_timeout': 30,
        }
        
        # Platform-specific configurations
        if platform == 'youtube':
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}
        elif platform == 'instagram':
            ydl_opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15'
            }
        elif platform == 'tiktok':
            ydl_opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        elif platform == 'facebook':
            ydl_opts['format'] = 'best'
        elif platform == 'twitter':
            ydl_opts['format'] = 'best'
        
        # Download the video
        print(f"Downloading from {platform}: {url}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        
        # Check if file exists
        if os.path.exists(filename):
            print(f"Download successful: {filename}")
            response = send_file(
                filename, 
                as_attachment=True, 
                download_name=os.path.basename(filename)
            )
            
            # Clean up file after sending
            try:
                os.remove(filename)
            except:
                pass
                
            return response
        else:
            return render_template('index.html', 
                                 message="Download failed - file not found", 
                                 error=True)
        
    except Exception as e:
        error_message = f"Download failed: {str(e)}"
        print(f"Download error: {error_message}")
        
        # Provide user-friendly error messages
        if "Sign in to confirm" in str(e):
            error_message = "YouTube is blocking downloads. Try a different video or platform."
        elif "Unable to extract" in str(e):
            error_message = "This platform is currently not working. Try another platform or update the link."
        elif "HTTP Error 403" in str(e):
            error_message = "Access denied. The video may be private or restricted."
        elif "Video unavailable" in str(e):
            error_message = "Video not found or unavailable."
        
        return render_template('index.html', 
                             message=error_message, 
                             error=True)

# For Vercel
app = app

if __name__ == "__main__":
    print("\n" + "="*50)
    print("🚀 Multi-Platform Video Downloader Starting...")
    print("="*50)
    print("📍 URL: http://127.0.0.1:5000")
    print("📍 Alternative: http://localhost:5000")
    print("⏹️  Press CTRL+C to stop")
    print("="*50)
    print("✅ Supported platforms:")
    print("   - YouTube")
    print("   - Facebook")
    print("   - TikTok")
    print("   - Twitter/X")
    print("   - Instagram")
    print("="*50 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000)