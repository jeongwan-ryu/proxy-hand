"""
작업차 서비스 (각 파이) — 담당: 유정완

하는 일: 선 따라가기, 정지선 세기, 정지선 놓침 확인(구간 시간, line_missed),
        손 명령을 micro:bit로 전달(arrived일 때만), 생존 신호 끊기면 안전 멈춤.
입력: car{n}/hand (UDP 5005), 관제 명령 (TCP 5006, 파이가 기다림), 선 센서, micro:bit 응답
출력: 바퀴 구동, 시리얼 P/M 명령, car{n}/status·arrived·error·reject
관련 SR: SR-05, SR-08, SR-10~13, SR-36, SR-38
참고: 시스템 설계 6.2·10장, 인터페이스 정의 4·7장
"""


def main():
    # TODO: 구현
    pass


if __name__ == "__main__":
    main()
