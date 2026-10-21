@echo off
rem 定期補齊全部股票的每日價格資料，由工作排程器每小時觸發一次
"C:\venv1\Scripts\python.exe" "D:\backup\myExam\stock\mystockweb1\manage.py" fetch_all_prices >> "D:\backup\myExam\stock\mystockweb1\scripts\fetch_all_prices.log" 2>&1
