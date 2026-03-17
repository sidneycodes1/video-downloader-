import yt_dlp
import os

def test_facebook_download(url):
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'quiet': False,
        'no_warnings': False,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    
    try:
        print(f"Testing download from: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print("Successfully extracted info!")
            print(f"Title: {info.get('title')}")
            print(f"Formats available: {len(info.get('formats', []))}")
            return True
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    # Using a known public video URL for testing
    test_url = "https://facebook.com/475841587884690/videos/744372871141776"
    test_facebook_download(test_url)
