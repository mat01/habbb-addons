ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip install --no-cache-dir boto3

COPY run.sh sync.py /
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
