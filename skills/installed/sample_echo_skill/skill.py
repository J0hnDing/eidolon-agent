import json
import sys

def main():
    payload = json.loads(sys.stdin.read() or '{}')
    print(json.dumps({'title': 'Sample Echo Skill', 'input': payload, 'warnings': []}))

if __name__ == '__main__':
    main()
