from flask import Flask, request, send_file, render_template, jsonify
from flask_cors import CORS
import yt_dlp
import os
import re
import tempfile

app = Flask(__name__)
CORS(app)  # Enable CORS for API requests

def validate_url(url):
    """Validate if the URL is properly formatted"""
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(url) is not None

def normalize_platform(platform):
    """Normalize platform name to lowercase"""
    platform_map = {
        'Instagram': 'instagram',
        'Facebook': 'facebook',
        'TikTok': 'tiktok',
        'X': 'twitter',
        'Twitter': 'twitter'
    }
    return platform_map.get(platform, platform.lower())

@app.route("/")
def home():
    return render_template('index.html')

@app.route("/download")
def download_page():
    """Render the download page"""
    return render_template("download.html")

@app.route("/api/download", methods=["POST"])
def download_video():
    """API endpoint for downloading videos"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        platform = normalize_platform(data.get('platform', 'youtube'))
        
        # Validate URL
        if not url:
            return jsonify({'error': 'Please provide a video URL'}), 400
        
        if not validate_url(url):
            return jsonify({'error': 'Invalid URL format. Please enter a valid URL.'}), 400
        
        # Determine downloads path (use temp directory)
        downloads_path = tempfile.gettempdir() if os.name != 'posix' else '/tmp'
        
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
            
            # Clean up file after sending (in background)
            try:
                os.remove(filename)
            except Exception as e:
                print(f"Error cleaning up file: {e}")
                
            return response
        else:
            return jsonify({'error': 'Download failed - file not found'}), 500
        
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
        elif "Private video" in str(e):
            error_message = "This video is private and cannot be downloaded."
        elif "Unsupported URL" in str(e):
            error_message = "Unsupported URL. Please check the link and try again."
        
        return jsonify({'error': error_message}), 500

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