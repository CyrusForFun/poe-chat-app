from flask import Flask, render_template, request, Response
import openai
import json
import os

app = Flask(__name__)

client = openai.OpenAI(
    api_key=os.environ.get("POE_API_KEY"),
    base_url="https://api.poe.com/v1",
)

MODELS = [
    "Claude-Sonnet-4.6",
    "Claude-Haiku-4.5",
    "GPT-4o",
    "GPT-4o-mini",
    "Gemini-2.5-Pro",
    "Gemini-2.5-Flash",
    "Llama-4-Maverick",
    "DeepSeek-R1",
]


@app.route("/")
def index():
    return render_template("index.html", models=MODELS)


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    messages = data.get("messages", [])
    model = data.get("model", "Claude-Sonnet-4.6")

    def generate():
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield f"data: {json.dumps({'content': chunk.choices[0].delta.content})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
