# Update Dependencies for Video Downloader
echo "Updating yt-dlp..."
python -m pip install --upgrade yt-dlp

echo ""
echo "Updating other dependencies..."
python -m pip install -r requirements.txt

echo ""
echo "Done! You can now run the app using run.bat"
pause
