import os
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
import time
import base64


def get_html_and_extract(link):
    try:
        response = requests.get(link)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        iframe = soup.find('iframe', id='ai-score')
        if iframe and 'src' in iframe.attrs:
            return iframe['src']
        else:
            return None
    except requests.RequestException as e:
        print(f"Error fetching the page: {e}")
        return None


def save_page_as_pdf(url, output_pdf):
    edge_options = Options()
    edge_options.use_chromium = True
    edge_options.add_argument("--headless")  # 无头模式
    edge_options.add_argument("--disable-gpu")
    edge_options.add_argument("--no-sandbox")
    edge_options.add_argument("--disable-dev-shm-usage")

    edge_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36")
    edge_options.add_argument("referer=")
    edge_options.add_argument("accept-language=en-US,en;q=0.9")

    driver_path = "msedgedriver.exe"
    service = Service(driver_path)
    driver = webdriver.Edge(service=service, options=edge_options)

    try:
        driver.get(url)
        script = """
        (function(){
            'use strict';
            if (!document.referrer) {
                location.href += '';
            }
            var style = document.createElement('style');
            style.innerHTML = '.print{display:none!important}';
            document.head.appendChild(style);
        })();
        """
        driver.execute_script(script)

        retries = 4
        for attempt in range(retries):
            time.sleep(5)
            elements = driver.find_elements(By.XPATH, "//*[name()='text' and @text-anchor='middle']")
            if elements:
                title = elements[0].text.strip()
                folder_name = title if title else "Unknown"
                os.makedirs(folder_name, exist_ok=True)

                print_options = {
                    'paperWidth': 8.27,
                    'paperHeight': 11.69,
                    'marginTop': 0,
                    'marginBottom': 0,
                    'marginLeft': 0,
                    'marginRight': 0,
                    'printBackground': True,
                    'landscape': False
                }

                pdf_data = driver.execute_cdp_cmd("Page.printToPDF", print_options)
                output_pdf_path = os.path.join(folder_name, f"{title}-{output_pdf}.pdf")
                with open(output_pdf_path, 'wb') as f:
                    f.write(base64.b64decode(pdf_data['data']))
                print(f"Page saved as PDF: {output_pdf_path}")
                return
            else:
                print(f"Attempt {attempt + 1} failed. Retrying...")

        print("Failed to load the page content completely.")
    finally:
        driver.quit()


if __name__ == "__main__":
    print("请输入要处理的链接，每行一个，输入空行结束：")
    links = []
    while True:
        link = input()
        if not link:
            break
        links.append(link)

    base_url = "https://www.gangqinpu.com"
    for link in links:
        print(f"Processing {link}")
        src_value = get_html_and_extract(link)
        if src_value:
            full_url = base_url + src_value

            # 五线谱模式
            save_page_as_pdf(full_url, "五线谱")

            # 简谱模式
            simplified_url = full_url.replace('jianpuMode=0', 'jianpuMode=1')
            save_page_as_pdf(simplified_url, "简谱")
        else:
            print(f"Failed to extract iframe src for {link}")
