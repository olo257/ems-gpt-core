FROM python:3.13-alpine
WORKDIR /app
RUN pip install --no-cache-dir PyMySQL==1.1.2
COPY *.py /app/
COPY webui.html /app/webui.html
COPY icon.png /app/icon.png
COPY run.sh /run.sh
RUN chmod 0755 /run.sh && mkdir -p /data
EXPOSE 8099
CMD ["/run.sh"]
