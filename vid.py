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
        
        # Create downloads folder
        downloads_path = os.path.join(os.getcwd(), 'downloads')
        if not os.path.exists(downloads_path):
            os.makedirs(downloads_path)
        
        # Configure yt-dlp options based on platform
        ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(downloads_path, '%(title)s.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
        }
        
        # Platform-specific configurations
        if platform == 'instagram':
            ydl_opts['format'] = 'best'
        elif platform == 'tiktok':
            ydl_opts['format'] = 'best'
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
            return send_file(filename, as_attachment=True, download_name=os.path.basename(filename))
        else:
            return render_template('index.html', 
                                 message="Download failed - file not found", 
                                 error=True)
        
    except Exception as e:
        error_message = f"Error: {str(e)}"
        print(f"Download error: {error_message}")
        return render_template('index.html', 
                             message=error_message, 
                             error=True)

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