import yt_dlp
import os

def test_platform_extraction(url, platform_name):
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
        # Omitting cookiesfrombrowser here as it might fail in restricted environments
    }
    
    try:
        print(f"\n--- Testing {platform_name} ---")
        print(f"URL: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"SUCCESS: Extracted '{info.get('title')}'")
            return True
    except Exception as e:
        print(f"FAILED: {e}")
        return False

if __name__ == "__main__":
    test_cases = [
        ("https://facebook.com/475841587884690/videos/744372871141776", "Facebook"),
        # Add other public URLs here if available
    ]
    
    for url, platform in test_cases:
        test_platform_extraction(url, platform)
