"""
Support for Alexa skill service end point.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/alexa/

"""
import asyncio
import enum
import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback
from homeassistant.helpers import intent
from homeassistant.components import http
from homeassistant.util.decorator import Registry

from .const import DOMAIN, SYN_RESOLUTION_MATCH

INTENTS_API_ENDPOINT = '/api/alexa'
HANDLERS = Registry()
_LOGGER = logging.getLogger(__name__)


class SpeechType(enum.Enum):
    """The Alexa speech types."""

    plaintext = "PlainText"
    ssml = "SSML"


SPEECH_MAPPINGS = {
    'plain': SpeechType.plaintext,
    'ssml': SpeechType.ssml,
}


class CardType(enum.Enum):
    """The Alexa card types."""

    simple = "Simple"
    link_account = "LinkAccount"


@callback
def async_setup(hass):
    """Activate Alexa component."""
    hass.http.register_view(AlexaIntentsView)


class UnknownRequest(HomeAssistantError):
    """When an unknown Alexa request is passed in."""


class AlexaIntentsView(http.HomeAssistantView):
    """Handle Alexa requests."""

    url = INTENTS_API_ENDPOINT
    name = 'api:alexa'

    @asyncio.coroutine
    def post(self, request):
        """Handle Alexa."""
        hass = request.app['hass']
        message = yield from request.json()

        _LOGGER.debug('Received Alexa request: %s', message)

        try:
            response = yield from async_handle_message(hass, message)
            return b'' if response is None else self.json(response)
        except UnknownRequest as err:
            _LOGGER.warning(str(err))
            return self.json(intent_error_response(
                hass, message, str(err)))

        except intent.UnknownIntent as err:
            _LOGGER.warning(str(err))
            return self.json(intent_error_response(
                hass, message,
                "This intent is not yet configured within Home Assistant."))

        except intent.InvalidSlotInfo as err:
            _LOGGER.error('Received invalid slot data from Alexa: %s', err)
            return self.json(intent_error_response(
                hass, message,
                "Invalid slot information received for this intent."))

        except intent.IntentError as err:
            _LOGGER.exception(str(err))
            return self.json(intent_error_response(
                hass, message, "Error handling intent."))


def intent_error_response(hass, message, error):
    """Return an Alexa response that will speak the error message."""
    alexa_intent_info = message.get('request').get('intent')
    alexa_response = AlexaResponse(hass, alexa_intent_info)
    alexa_response.add_speech(SpeechType.plaintext, error)
    return alexa_response.as_dict()


@asyncio.coroutine
def async_handle_message(hass, message):
    """Handle an Alexa intent.

    Raises:
     - UnknownRequest
     - intent.UnknownIntent
     - intent.InvalidSlotInfo
     - intent.IntentError
    """
    req = message.get('request')
    req_type = req['type']

    handler = HANDLERS.get(req_type)

    if not handler:
        raise UnknownRequest('Received unknown request {}'.format(req_type))

    return (yield from handler(hass, message))


@HANDLERS.register('SessionEndedRequest')
@asyncio.coroutine
def async_handle_session_end(hass, message):
    """Handle a session end request."""
    return None


@HANDLERS.register('IntentRequest')
@HANDLERS.register('LaunchRequest')
@asyncio.coroutine
def async_handle_intent(hass, message):
    """Handle an intent request.

    Raises:
     - intent.UnknownIntent
     - intent.InvalidSlotInfo
     - intent.IntentError
    """
    req = message.get('request')
    alexa_intent_info = req.get('intent')
    alexa_response = AlexaResponse(hass, alexa_intent_info)

    if req['type'] == 'LaunchRequest':
        intent_name = message.get('session', {})  \
                             .get('application', {})  \
                             .get('applicationId')
    else:
        intent_name = alexa_intent_info['name']

    intent_response = yield from intent.async_handle(
        hass, DOMAIN, intent_name,
        {key: {'value': value} for key, value
         in alexa_response.variables.items()})

    for intent_speech, alexa_speech in SPEECH_MAPPINGS.items():
        if intent_speech in intent_response.speech:
            alexa_response.add_speech(
                alexa_speech,
                intent_response.speech[intent_speech]['speech'])
            break

    if 'simple' in intent_response.card:
        alexa_response.add_card(
            CardType.simple, intent_response.card['simple']['title'],
            intent_response.card['simple']['content'])

    return alexa_response.as_dict()


def resolve_slot_synonyms(key, request):
    """Check slot request for synonym resolutions."""
    # Default to the spoken slot value if more than one or none are found. For
    # reference to the request object structure, see the Alexa docs:
    # https://tinyurl.com/ybvm7jhs
    resolved_value = request['value']

    if ('resolutions' in request and
            'resolutionsPerAuthority' in request['resolutions'] and
            len(request['resolutions']['resolutionsPerAuthority']) >= 1):

        # Extract all of the possible values from each authority with a
        # successful match
        possible_values = []

        for entry in request['resolutions']['resolutionsPerAuthority']:
            if entry['status']['code'] != SYN_RESOLUTION_MATCH:
                continue

            possible_values.extend([item['value']['name']
                                    for item
                                    in entry['values']])

        # If there is only one match use the resolved value, otherwise the
        # resolution cannot be determined, so use the spoken slot value
        if len(possible_values) == 1:
            resolved_value = possible_values[0]
        else:
            _LOGGER.debug(
                'Found multiple synonym resolutions for slot value: {%s: %s}',
                key,
                request['value']
            )

    return resolved_value


class AlexaResponse(object):
    """Help generating the response for Alexa."""

    def __init__(self, hass, intent_info):
        """Initialize the response."""
        self.hass = hass
        self.speech = None
        self.card = None
        self.reprompt = None
        self.session_attributes = {}
        self.should_end_session = True
        self.variables = {}

        # Intent is None if request was a LaunchRequest or SessionEndedRequest
        if intent_info is not None:
            for key, value in intent_info.get('slots', {}).items():
                # Only include slots with values
                if 'value' not in value:
                    continue

                _key = key.replace('.', '_')

                self.variables[_key] = resolve_slot_synonyms(key, value)

    def add_card(self, card_type, title, content):
        """Add a card to the response."""
        assert self.card is None

        card = {
            "type": card_type.value
        }

        if card_type == CardType.link_account:
            self.card = card
            return

        card["title"] = title
        card["content"] = content
        self.card = card

    def add_speech(self, speech_type, text):
        """Add speech to the response."""
        assert self.speech is None

        key = 'ssml' if speech_type == SpeechType.ssml else 'text'

        self.speech = {
            'type': speech_type.value,
            key: text
        }

    def add_reprompt(self, speech_type, text):
        """Add reprompt if user does not answer."""
        assert self.reprompt is None

        key = 'ssml' if speech_type == SpeechType.ssml else 'text'

        self.reprompt = {
            'type': speech_type.value,
            key: text.async_render(self.variables)
        }

    def as_dict(self):
        """Return response in an Alexa valid dict."""
        response = {
            'shouldEndSession': self.should_end_session
        }

        if self.card is not None:
            response['card'] = self.card

        if self.speech is not None:
            response['outputSpeech'] = self.speech

        if self.reprompt is not None:
            response['reprompt'] = {
                'outputSpeech': self.reprompt
            }

        return {
            'version': '1.0',
            'sessionAttributes': self.session_attributes,
            'response': response,
        }
