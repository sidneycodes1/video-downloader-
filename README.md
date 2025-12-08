# Video Downloader - Installation & Usage Guide

A multi-platform video downloader supporting YouTube, Instagram, Facebook, TikTok, and Twitter/X.

## 📋 Prerequisites

- Python 3.7 or higher (you have Python 3.14.0 ✅)
- pip (Python package installer)

## 🚀 Installation Steps

### Option 1: Quick Install (Recommended for Getting Started)

1. **Open PowerShell or Command Prompt** in the project directory

2. **Install dependencies:**
   ```bash
   pip install Flask==3.0.0 flask-cors==4.0.0 yt-dlp
   ```
   
   Or using requirements.txt:
   ```bash
   pip install -r requirements.txt
   ```

### Option 2: Using Virtual Environment (Best Practice)

1. **Create a virtual environment:**
   ```bash
   python -m venv venv
   ```

2. **Activate the virtual environment:**
   
   **On Windows (PowerShell):**
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```
   
   **On Windows (Command Prompt):**
   ```cmd
   venv\Scripts\activate.bat
   ```
   
   **On Mac/Linux:**
   ```bash
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## ▶️ Running the Application

1. **Make sure you're in the project directory:**
   ```bash
   cd "C:\Users\USER\Documents\MY CODES\vid download"
   ```

2. **Run the Flask application:**
   ```bash
   python vid.py
   ```

3. **Open your web browser** and navigate to:
   - **Main URL:** http://127.0.0.1:5000
   - **Alternative:** http://localhost:5000

4. **You should see:**
   - A landing page with platform selection (Instagram, Facebook, TikTok, X)
   - Click on a platform to go to the download page
   - Paste a video URL and click download

## 🛑 Stopping the Application

Press `CTRL + C` in the terminal to stop the server.

## 📝 Usage Instructions

1. **Select a Platform:**
   - On the landing page, click on one of the platform icons (Instagram, Facebook, TikTok, or X)

2. **Download a Video:**
   - Paste the video URL in the input field
   - Click the download button or press Enter
   - Wait for the download to complete (you'll see a loading spinner)
   - The video will automatically download to your default downloads folder

## ⚠️ Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'flask'"
**Solution:** Install Flask and dependencies:
```bash
pip install Flask flask-cors yt-dlp
```

### Issue: "Port already in use"
**Solution:** Either:
- Stop the other application using port 5000, or
- Change the port in `vid.py` (line 124): `app.run(debug=True, host='0.0.0.0', port=5001)`

### Issue: PowerShell execution policy error
**Solution:** Run this command in PowerShell as Administrator:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Issue: Download fails
**Possible causes:**
- Invalid or unsupported video URL
- Video is private or restricted
- Network connection issues
- Platform-specific restrictions

## 📦 Project Structure

```
vid download/
├── vid.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── templates/
│   ├── index.html        # Landing page
│   └── download.html     # Download page
├── static/
│   └── css/
│       └── style.css     # Stylesheet
└── README.md            # This file
```

## 🌐 Supported Platforms

- ✅ YouTube
- ✅ Instagram
- ✅ Facebook
- ✅ TikTok
- ✅ Twitter/X

## 📄 License

This project is for educational purposes.

---

**Need Help?** Check the error messages in the terminal for detailed information about any issues.

